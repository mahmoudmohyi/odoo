# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import itertools
import pytz

from psycopg2 import IntegrityError
from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _
from odoo.addons.resource.models.resource import Intervals
from odoo.exceptions import ValidationError
from odoo.tools import mute_logger


class HrBenefit(models.Model):
    _name = 'hr.benefit'
    _description = 'hr.benefit'
    _order = 'display_warning desc,state,date_start'

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    employee_id = fields.Many2one('hr.employee', required=True,
        domain=lambda self: [('contract_ids.state', 'in', ('open', 'pending')), ('company_id', '=', self.env.user.company_id.id)])
    date_start = fields.Datetime(required=True, string='Start')
    date_stop = fields.Datetime(string='End')
    duration = fields.Float(compute='_compute_duration', inverse='_inverse_duration', store=True, string="Hours")
    benefit_type_id = fields.Many2one('hr.benefit.type')
    color = fields.Integer(related='benefit_type_id.color', readonly=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('confirmed', 'Confirmed'),
        ('validated', 'Validated'),
        ('cancelled', 'Cancelled')
    ], default='draft')
    display_warning = fields.Boolean(string="Error")
    leave_id = fields.Many2one('hr.leave', string='Leave')

    _sql_constraints = [
        ('_unique', 'unique (date_start, date_stop, employee_id, benefit_type_id)', "Benefit already exists for this attendance"),
        ('_benefit_has_end', 'check (date_stop IS NOT NULL OR duration <> 0)', 'Benefit must end. Please define an end date or a duration.'),
        ('_benefit_start_before_end', 'check (date_stop is null OR (date_stop > date_start))', 'Starting time should be before end time.')
    ]

    @api.onchange('duration')
    def _onchange_duration(self):
        self._inverse_duration()

    def _get_duration(self, date_start, date_stop):
        if not date_start or not date_stop:
            return 0
        dt = date_stop - date_start
        return dt.days * 24 + dt.seconds / 3600 # Number of hours

    @api.depends('date_stop', 'date_start')
    def _compute_duration(self):
        for benefit in self:
            benefit.duration = benefit._get_duration(benefit.date_start, benefit.date_stop)

    def _inverse_duration(self):
        for benefit in self:
            if benefit.date_start and benefit.duration:
                benefit.date_stop = benefit.date_start + relativedelta(hours=benefit.duration)

    def write(self, vals):
        if 'state' in vals:
            if vals['state'] == 'draft':
                vals['active'] = True
            if vals['state'] == 'cancelled':
                vals['active'] = False
        return super(HrBenefit, self).write(vals)

    @api.multi
    def _check_if_error(self):
        if not self:
            return False
        undefined_type = self.filtered(lambda b: not b.benefit_type_id)
        undefined_type.write({'display_warning': True})
        conflict = self._compute_schedule_conflicts()
        conflict_with_leaves = self._compute_conflicts_leaves_to_approve()
        return undefined_type or conflict or conflict_with_leaves

    @api.multi
    def _compute_schedule_conflicts(self):
        conflict = False
        date_start_benefits = min(self.mapped('date_start'))
        date_stop_benefits = max(self.mapped('date_stop'))
        domain = [
            ('date_start', '<', date_stop_benefits),
            ('date_stop', '>', date_start_benefits),
        ]

        benefs = self.search(domain)
        benefits_by_employee = itertools.groupby(benefs, lambda b: b.employee_id)
        for employee, benefs in benefits_by_employee:
            intervals = Intervals(intervals=((b.date_start, b.date_stop, b) for b in benefs))
            for interval in intervals:
                if len(interval[2]) > 1:
                    interval[2].write({'display_warning': True})
                    conflict = True
        return conflict

    @api.multi
    def _compute_conflicts_leaves_to_approve(self):
        if not self:
            return False

        query = """
            SELECT
                b.id AS benefit_id,
                l.id AS leave_id
            FROM hr_benefit b
            INNER JOIN hr_leave l ON b.employee_id = l.employee_id
            WHERE
                b.id IN %s AND
                l.date_from <= b.date_stop AND
                l.date_to >= b.date_start AND
                l.state IN ('confirm', 'validate1');
        """
        self.env.cr.execute(query, [tuple(self.ids)])
        conflicts = self.env.cr.dictfetchall()
        for res in conflicts:
            self.browse(res.get('benefit_id')).write({
                'display_warning': True,
                'leave_id': res.get('leave_id')
            })
        return bool(conflicts)

    def _safe_duplicate_create(self, vals_list, date_start, date_stop):
        """
        Create benefits between date_start and date_stop according to vals_list.
        Skip the values in vals_list if a benefit already exists for the given
        date_start, date_stop, employee_id, benefit_type_id
        :return: new record id if it didn't exist.
        """
        # The search_read should be fast as date_start and date_stop are indexed from the
        # unique sql constraint
        month_recs = self.search_read([('date_start', '>=', date_start), ('date_stop', '<=', date_stop)],
                                      ['employee_id', 'date_start', 'date_stop', 'benefit_type_id'])
        existing_entries = {(
            r['date_start'],
            r['date_stop'],
            r['employee_id'][0],
            r['benefit_type_id'][0] if r['benefit_type_id'] else False,
        ) for r in month_recs}
        new_vals = [v for v in vals_list if (v['date_start'].replace(tzinfo=None), v['date_stop'].replace(tzinfo=None), v['employee_id'], v['benefit_type_id']) not in existing_entries]
        # Remove duplicates from vals_list, shouldn't be necessary from saas-12.2
        unique_new_vals = set()
        for values in new_vals:
            unique_new_vals.add(tuple(values.items()))
        new_vals = [dict(values) for values in unique_new_vals]
        return self.create(new_vals)

    def action_leave(self):
        leave = self.leave_id
        return {
            'type': 'ir.actions.act_window',
            'view_type': 'form',
            'view_mode': 'form',
            'res_id': leave.id,
            'res_model': 'hr.leave',
            'views': [[False, 'form']],
        }

    def _split_by_day(self):
        """
        Split the benefit by days and unlink the original benefit.
        @return recordset
        """
        def _split_range_by_day(start, end):
            days = []
            current_start = start
            current_end = start.replace(hour=23, minute=59, second=59)
            while current_end < end:
                days.append((current_start, current_end))
                current_start = current_end + relativedelta(seconds=1)
                current_end = current_end + relativedelta(days=1)

            days.append((current_start, end))

            # filter to avoid dummy intervals starting and ending at the same time
            return [(start, end) for start, end in days if start != end]

        new_benefits = self.env['hr.benefit']
        for benefit in self:
            if benefit.date_start.date() == benefit.date_stop.date():
                new_benefits |= benefit
            else:
                tz = pytz.timezone(benefit.employee_id.tz)
                benefit_start, benefit_stop = tz.localize(benefit.date_start), tz.localize(benefit.date_stop)
                values = {
                    'name': benefit.name,
                    'employee_id': benefit.employee_id.id,
                    'benefit_type_id': benefit.benefit_type_id.id,
                }
                benefit_state = benefit.state
                benefit.unlink()
                for start, stop in _split_range_by_day(benefit_start, benefit_stop):
                    values['date_start'] = start.astimezone(pytz.utc)
                    values['date_stop'] = stop.astimezone(pytz.utc)
                    new_benefit = self.create(values)
                    # Write the state after the creation due to the ir.rule on benefit state
                    new_benefit.state = benefit_state
                    new_benefits |= new_benefit

        return new_benefits

    @api.multi
    def _duplicate_to_calendar(self):
        """
            Duplicate data to keep the complexity in benefit and not mess up payroll, etc.
        """
        attendance_benefits = self.filtered(lambda b:
            not b.benefit_type_id.is_leave and
            # Normal benefit are global to all employees -> avoid duplicating it
            not b.benefit_type_id == self.env.ref('hr_payroll.benefit_type_attendance'))
        leave_benefits = self.filtered(lambda b: b.benefit_type_id.is_leave)

        for benefit in attendance_benefits:
            benefit = benefit._split_by_day()
            benefit._duplicate_to_calendar_attendance()
        leave_benefits._duplicate_to_calendar_leave()

    @api.multi
    def _duplicate_to_calendar_leave(self):

        for benefit in self:
            if not benefit.leave_id:
                self.env['resource.calendar.leaves'].create({
                    'name': benefit.name,
                    'date_from': benefit.date_start,
                    'date_to': benefit.date_stop,
                    'calendar_id': benefit.employee_id.resource_calendar_id.id,
                    'resource_id': benefit.employee_id.resource_id.id,
                    'benefit_type_id': benefit.benefit_type_id.id,
                })

    @api.multi
    def _duplicate_to_calendar_attendance(self):
        mapped_data = {
            benefit: [
                pytz.utc.localize(benefit.date_start).astimezone(pytz.timezone(benefit.employee_id.tz)), # Start date
                pytz.utc.localize(benefit.date_stop).astimezone(pytz.timezone(benefit.employee_id.tz)) # End date
            ] for benefit in self
        }

        if any(data[0].date() != data[1].date() for data in mapped_data.values()):
            raise ValidationError(_("You can't validate a benefit that covers several days."))

        for benefit in self:
            start, end = mapped_data.get(benefit)

            self.env['resource.calendar.attendance'].create({
                'name': benefit.name,
                'dayofweek': str(start.weekday()),
                'date_from': start.date(),
                'date_to': end.date(),
                'hour_from': start.hour + start.minute / 60,
                'hour_to': end.hour + end.minute / 60,
                'calendar_id': benefit.employee_id.resource_calendar_id.id,
                'day_period': 'morning' if end.hour <= 12 else 'afternoon',
                'resource_id': benefit.employee_id.resource_id.id,
                'benefit_type_id': benefit.benefit_type_id.id,
            })

    @api.model
    def action_validate(self, ids):
        benefits = self.env['hr.benefit'].search([('id', 'in', ids), ('state', '!=', 'validated')])
        benefits.write({'display_warning': False})
        if not benefits._check_if_error():
            benefits.write({'state': 'validated'})
            benefits._duplicate_to_calendar()
            return True
        return False


class HrBenefitType(models.Model):
    _name = 'hr.benefit.type'
    _description = 'hr.benefit.type'

    name = fields.Char(required=True)
    code = fields.Char()
    color = fields.Integer(default=1) # Will be used with the new calendar/kanban view
    sequence = fields.Integer(default=25)
    active = fields.Boolean(
        'Active', default=True,
        help="If the active field is set to false, it will allow you to hide the benefit type without removing it.")
    is_leave = fields.Boolean(default=False, string="Leave")
