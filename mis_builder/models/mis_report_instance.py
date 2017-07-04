# -*- coding: utf-8 -*-
# © 2014-2016 ACSONE SA/NV (<http://acsone.eu>)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

import datetime
import logging

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError

from .aep import AccountingExpressionProcessor as AEP

_logger = logging.getLogger(__name__)


SRC_ACTUALS = 'actuals'
SRC_ACTUALS_ALT = 'actuals_alt'
SRC_CMPCOL = 'cmpcol'
SRC_SUMCOL = 'sumcol'

MODE_NONE = 'none'
MODE_FIX = 'fix'
MODE_REL = 'relative'


class MisReportInstancePeriodSum(models.Model):

    _name = "mis.report.instance.period.sum"

    period_id = fields.Many2one(
        comodel_name='mis.report.instance.period',
        string="Parent column",
        ondelete='cascade',
        required=True,
    )
    period_to_sum_id = fields.Many2one(
        comodel_name='mis.report.instance.period',
        string="Column",
        ondelete='restrict',
        required=True,
    )
    sign = fields.Selection(
        [('+', '+'),
         ('-', '-')],
        required=True,
        default='+',
    )

    @api.constrains('period_id', 'period_to_sum_id')
    def _check_period_to_sum(self):
        if self.period_id == self.period_to_sum_id:
            raise ValidationError(
                _("You cannot sum period %s with itself.") %
                self.period_id.name)


class MisReportInstancePeriod(models.Model):
    """ A MIS report instance has the logic to compute
    a report template for a given date period.

    Periods have a duration (day, week, fiscal period) and
    are defined as an offset relative to a pivot date.
    """

    @api.multi
    @api.depends('report_instance_id.pivot_date',
                 'report_instance_id.comparison_mode',
                 'date_range_type_id',
                 'type', 'offset', 'duration', 'mode',
                 'manual_date_from', 'manual_date_to')
    def _compute_dates(self):
        for record in self:
            record.date_from = False
            record.date_to = False
            record.valid = False
            report = record.report_instance_id
            d = fields.Date.from_string(report.pivot_date)
            if not report.comparison_mode:
                record.date_from = report.date_from
                record.date_to = report.date_to
                record.valid = record.date_from and record.date_to
            elif record.mode == MODE_NONE:
                record.date_from = False
                record.date_to = False
                record.valid = True
            elif record.mode == MODE_FIX:
                record.date_from = record.manual_date_from
                record.date_to = record.manual_date_to
                record.valid = record.date_from and record.date_to
            elif record.type == 'd':
                date_from = d + datetime.timedelta(days=record.offset)
                date_to = date_from + \
                    datetime.timedelta(days=record.duration - 1)
                record.date_from = fields.Date.to_string(date_from)
                record.date_to = fields.Date.to_string(date_to)
                record.valid = True
            elif record.type == 'w':
                date_from = d - datetime.timedelta(d.weekday())
                date_from = date_from + \
                    datetime.timedelta(days=record.offset * 7)
                date_to = date_from + \
                    datetime.timedelta(days=(7 * record.duration) - 1)
                record.date_from = fields.Date.to_string(date_from)
                record.date_to = fields.Date.to_string(date_to)
                record.valid = True
            elif record.type == 'date_range':
                date_range_obj = record.env['date.range']
                current_periods = date_range_obj.search(
                    [('type_id', '=', record.date_range_type_id.id),
                     ('date_start', '<=', d),
                     ('date_end', '>=', d),
                     ('company_id', 'in',
                      record.report_instance_id.company_ids.ids)])
                if current_periods:
                    all_periods = date_range_obj.search(
                        [('type_id', '=', record.date_range_type_id.id),
                         ('company_id', 'in',
                          record.report_instance_id.company_ids.ids)],
                        order='date_start')
                    all_period_ids = [p.id for p in all_periods]
                    p = all_period_ids.index(current_periods[0].id) + \
                        record.offset
                    if p >= 0 and p + record.duration <= len(all_period_ids):
                        periods = all_periods[p:p + record.duration]
                        record.date_from = periods[0].date_start
                        record.date_to = periods[-1].date_end
                        record.valid = True

    _name = 'mis.report.instance.period'

    name = fields.Char(size=32, required=True,
                       string='Label', translate=True)
    mode = fields.Selection([(MODE_FIX, 'Fixed dates'),
                             (MODE_REL, 'Relative to report base date'),
                             (MODE_NONE, 'No date filter'),
                             ], required=True,
                            default=MODE_FIX)
    type = fields.Selection([('d', _('Day')),
                             ('w', _('Week')),
                             ('date_range', _('Date Range'))
                             ],
                            string='Period type')
    date_range_type_id = fields.Many2one(
        comodel_name='date.range.type', string='Date Range Type')
    offset = fields.Integer(string='Offset',
                            help='Offset from current period',
                            default=-1)
    duration = fields.Integer(string='Duration',
                              help='Number of periods',
                              default=1)
    date_from = fields.Date(compute='_compute_dates', string="From")
    date_to = fields.Date(compute='_compute_dates', string="To")
    manual_date_from = fields.Date(string="From")
    manual_date_to = fields.Date(string="To")
    date_range_id = fields.Many2one(
        comodel_name='date.range',
        string='Date Range')
    valid = fields.Boolean(compute='_compute_dates',
                           type='boolean',
                           string='Valid')
    sequence = fields.Integer(string='Sequence', default=100)
    report_instance_id = fields.Many2one(comodel_name='mis.report.instance',
                                         string='Report Instance',
                                         required=True,
                                         ondelete='cascade')
    report_id = fields.Many2one(
        related='report_instance_id.report_id'
    )
    normalize_factor = fields.Integer(
        string='Factor',
        help='Factor to use to normalize the period (used in comparison',
        default=1)
    subkpi_ids = fields.Many2many(
        'mis.report.subkpi',
        string="Sub KPI Filter")

    source = fields.Selection(
        [(SRC_ACTUALS, 'Actuals'),
         (SRC_ACTUALS_ALT, 'Actuals (alternative)'),
         (SRC_SUMCOL, 'Sum columns'),
         (SRC_CMPCOL, 'Compare columns')],
        default=SRC_ACTUALS,
        required=True,
        help="Actuals: current data, from accounting and other queries.\n"
             "Actuals (alternative): current data from an "
             "alternative source (eg a database view providing look-alike "
             "account move lines).\n"
             "Sum columns: summation (+/-) of other columns.\n"
             "Compare to column: compare to other column.\n",
    )
    source_aml_model_id = fields.Many2one(
        comodel_name='ir.model',
        string='Move lines source',
        domain=[('field_id.name', '=', 'debit'),
                ('field_id.name', '=', 'credit'),
                ('field_id.name', '=', 'account_id'),
                ('field_id.name', '=', 'date')],
        help="A 'move line like' model, ie having at least debit, credit, "
             "date and account_id fields.",
    )
    source_sumcol_ids = fields.One2many(
        comodel_name='mis.report.instance.period.sum',
        inverse_name='period_id',
        string='Columns to sum',
    )
    source_sumcol_accdet = fields.Boolean(
        string='Sum account details',
    )
    source_cmpcol_from_id = fields.Many2one(
        comodel_name='mis.report.instance.period',
        string='versus',
    )
    source_cmpcol_to_id = fields.Many2one(
        comodel_name='mis.report.instance.period',
        string='Compare',
    )

    _order = 'sequence, id'

    _sql_constraints = [
        ('duration', 'CHECK (duration>0)',
         'Wrong duration, it must be positive!'),
        ('normalize_factor', 'CHECK (normalize_factor>0)',
         'Wrong normalize factor, it must be positive!'),
        ('name_unique', 'unique(name, report_instance_id)',
         'Period name should be unique by report'),
    ]

    @api.onchange('date_range_id')
    def _onchange_date_range(self):
        if self.date_range_id:
            self.manual_date_from = self.date_range_id.date_start
            self.manual_date_to = self.date_range_id.date_end

    @api.onchange('manual_date_from', 'manual_date_to')
    def _onchange_dates(self):
        if self.date_range_id:
            if self.manual_date_from != self.date_range_id.date_start or \
                    self.manual_date_to != self.date_range_id.date_end:
                self.date_range_id = False

    @api.onchange('source')
    def _onchange_source(self):
        if self.source in (SRC_SUMCOL, SRC_CMPCOL):
            self.mode = MODE_NONE

    @api.multi
    def _get_additional_move_line_filter(self):
        """ Prepare a filter to apply on all move lines

        This filter is applied with a AND operator on all
        accounting expression domains. This hook is intended
        to be inherited, and is useful to implement filtering
        on analytic dimensions or operational units.

        Returns an Odoo domain expression (a python list)
        compatible with account.move.line."""
        self.ensure_one()
        return []

    @api.multi
    def _get_additional_query_filter(self, query):
        """ Prepare an additional filter to apply on the query

        This filter is combined to the query domain with a AND
        operator. This hook is intended
        to be inherited, and is useful to implement filtering
        on analytic dimensions or operational units.

        Returns an Odoo domain expression (a python list)
        compatible with the model of the query."""
        self.ensure_one()
        return []

    @api.constrains('mode', 'source')
    def _check_mode_source(self):
        if self.source in (SRC_ACTUALS, SRC_ACTUALS_ALT):
            if self.mode == MODE_NONE:
                raise ValidationError(
                    _("A date filter is mandatory for this source "
                      "in column %s.") % self.name)
        elif self.source in (SRC_SUMCOL, SRC_CMPCOL):
            if self.mode != MODE_NONE:
                raise ValidationError(
                    _("No date filter is allowed for this source "
                      "in column %s.") % self.name)

    @api.constrains('source', 'source_cmpcol_from_id', 'source_cmpcol_to_id')
    def _check_source_cmpcol(self):
        if self.source == SRC_CMPCOL:
            if not self.source_cmpcol_from_id or \
                    not self.source_cmpcol_to_id:
                raise ValidationError(
                    _("Please provide both columns to compare in %s.") %
                    self.name)
            if self.source_cmpcol_from_id == self.id or \
                    self.source_cmpcol_to_id == self.id:
                raise ValidationError(
                    _("Column %s cannot be compared to itself.") %
                    self.name)
            if self.source_cmpcol_from_id.report_instance_id != \
                    self.report_instance_id or \
                    self.source_cmpcol_to_id.report_instance_id != \
                    self.report_instance_id:
                raise ValidationError(
                    _("Columns to compare must belong to the same report "
                      "in %s") % self.name)


class MisReportInstance(models.Model):
    """The MIS report instance combines everything to compute
    a MIS report template for a set of periods."""

    @api.depends('date')
    def _compute_pivot_date(self):
        for record in self:
            if record.date:
                record.pivot_date = record.date
            else:
                record.pivot_date = fields.Date.context_today(record)

    @api.model
    def _default_company_ids(self):
        return [(6, 0, [self.env['res.company'].
                _company_default_get('mis.report.instance').id])]

    _name = 'mis.report.instance'

    name = fields.Char(required=True,
                       string='Name', translate=True)
    description = fields.Char(related='report_id.description',
                              readonly=True)
    date = fields.Date(string='Base date',
                       help='Report base date '
                            '(leave empty to use current date)')
    pivot_date = fields.Date(compute='_compute_pivot_date',
                             string="Pivot date")
    report_id = fields.Many2one('mis.report',
                                required=True,
                                string='Report')
    period_ids = fields.One2many(comodel_name='mis.report.instance.period',
                                 inverse_name='report_instance_id',
                                 required=True,
                                 string='Periods',
                                 copy=True)
    target_move = fields.Selection([('posted', 'All Posted Entries'),
                                    ('all', 'All Entries')],
                                   string='Target Moves',
                                   required=True,
                                   default='posted')
    company_ids = fields.Many2many(
        comodel_name='res.company',
        string='Companies',
        help='Select companies for which data will  be searched. \
            User\'s company by default.',
        default=_default_company_ids,
        required=True)
    currency_id = fields.Many2one(
        comodel_name='res.currency',
        string='Currency',
        help='Select currency target for the report. \
            User\'s company currency by default.',
        required=False)
    exchange_rate_date = fields.Selection(
        [('now', _('Now')),
         ('start', _('Start of period')),
         ('end', _('End of period')),
         ('daily', _('Daily rate'))],
        string='Exchange rate date',
        default='now')
    landscape_pdf = fields.Boolean(string='Landscape PDF')
    comparison_mode = fields.Boolean(
        compute="_compute_comparison_mode",
        inverse="_inverse_comparison_mode")
    date_range_id = fields.Many2one(
        comodel_name='date.range',
        string='Date Range')
    date_from = fields.Date(string="From")
    date_to = fields.Date(string="To")
    temporary = fields.Boolean(default=False)

    @api.multi
    def save_report(self):
        self.ensure_one()
        self.write({'temporary': False})
        action = self.env.ref('mis_builder.mis_report_instance_view_action')
        res = action.read()[0]
        view = self.env.ref('mis_builder.mis_report_instance_view_form')
        res.update({
            'views': [(view.id, 'form')],
            'res_id': self.id,
            })
        return res

    @api.model
    def _vacuum_report(self, hours=24):
        clear_date = fields.Datetime.to_string(
            datetime.datetime.now() - datetime.timedelta(hours=hours))
        reports = self.search([
            ('write_date', '<', clear_date),
            ('temporary', '=', True),
            ])
        _logger.debug('Vacuum %s Temporary MIS Builder Report', len(reports))
        return reports.unlink()

    @api.multi
    def copy(self, default=None):
        self.ensure_one()
        default = dict(default or {})
        default['name'] = _('%s (copy)') % self.name
        return super(MisReportInstance, self).copy(default)

    def _format_date(self, date):
        # format date following user language
        lang_model = self.env['res.lang']
        lang = lang_model._lang_get(self.env.user.lang)
        date_format = lang.date_format
        return datetime.datetime.strftime(
            fields.Date.from_string(date), date_format)

    @api.multi
    @api.depends('date_from')
    def _compute_comparison_mode(self):
        for instance in self:
            instance.comparison_mode = bool(instance.period_ids) and\
                not bool(instance.date_from)

    @api.multi
    def _inverse_comparison_mode(self):
        for record in self:
            if not record.comparison_mode:
                if not record.date_from:
                    record.date_from = datetime.now()
                if not record.date_to:
                    record.date_to = datetime.now()
                record.period_ids.unlink()
                record.write({'period_ids': [
                    (0, 0, {
                        'name': 'Default',
                        'type': 'd',
                        })
                    ]})
            else:
                record.date_from = None
                record.date_to = None

    @api.onchange('date_range_id')
    def _onchange_date_range(self):
        if self.date_range_id:
            self.date_from = self.date_range_id.date_start
            self.date_to = self.date_range_id.date_end

    @api.onchange('date_from', 'date_to')
    def _onchange_dates(self):
        if self.date_range_id:
            if self.date_from != self.date_range_id.date_start or \
                    self.date_to != self.date_range_id.date_end:
                self.date_range_id = False

    @api.multi
    def preview(self):
        self.ensure_one()
        view_id = self.env.ref('mis_builder.'
                               'mis_report_instance_result_view_form')
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'mis.report.instance',
            'res_id': self.id,
            'view_mode': 'form',
            'view_type': 'form',
            'view_id': view_id.id,
            'target': 'current',
        }

    @api.multi
    def print_pdf(self):
        self.ensure_one()
        return {
            'name': 'MIS report instance QWEB PDF report',
            'model': 'mis.report.instance',
            'type': 'ir.actions.report.xml',
            'report_name': 'mis_builder.report_mis_report_instance',
            'report_type': 'qweb-pdf',
            'context': self.env.context,
        }

    @api.multi
    def export_xls(self):
        self.ensure_one()
        return {
            'name': 'MIS report instance XLSX report',
            'model': 'mis.report.instance',
            'type': 'ir.actions.report.xml',
            'report_name': 'mis.report.instance.xlsx',
            'report_type': 'xlsx',
            'context': self.env.context,
        }

    @api.multi
    def display_settings(self):
        assert len(self.ids) <= 1
        view_id = self.env.ref('mis_builder.mis_report_instance_view_form')
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'mis.report.instance',
            'res_id': self.id if self.id else False,
            'view_mode': 'form',
            'view_type': 'form',
            'views': [(view_id.id, 'form')],
            'view_id': view_id.id,
            'target': 'current',
        }

    def _add_column_actuals(
            self, aep, kpi_matrix, period, label, description):
        if not period.date_from or not period.date_to:
            raise UserError(_("Column %s with actuals source "
                              "must have from/to dates." %
                            (period.name,)))
        self.report_id.declare_and_compute_period(
            kpi_matrix,
            period.id,
            label,
            description,
            aep,
            period.date_from,
            period.date_to,
            self.target_move,
            period.subkpi_ids,
            period._get_additional_move_line_filter,
            period._get_additional_query_filter)

    def _add_column_actuals_alt(
            self, aep, kpi_matrix, period, label, description):
        if not period.date_from or not period.date_to:
            raise UserError(_("Column %s with actuals source "
                              "must have from/to dates." %
                            (period.name,)))
        self.report_id.declare_and_compute_period(
            kpi_matrix,
            period.id,
            label,
            description,
            aep,
            period.date_from,
            period.date_to,
            None,
            period.subkpi_ids,
            period._get_additional_move_line_filter,
            period._get_additional_query_filter,
            aml_model=period.source_aml_model_id.model)

    def _add_column_sumcol(
            self, aep, kpi_matrix, period, label, description):
        kpi_matrix.declare_sum(
            period.id,
            [(c.sign, c.period_to_sum_id.id)
             for c in period.source_sumcol_ids],
            label, description, period.source_sumcol_accdet)

    def _add_column_cmpcol(
            self, aep, kpi_matrix, period, label, description):
        kpi_matrix.declare_comparison(
            period.id,
            period.source_cmpcol_to_id.id, period.source_cmpcol_from_id.id,
            label, description)

    def _add_column(self, aep, kpi_matrix, period, label, description):
        if period.source == SRC_ACTUALS:
            return self._add_column_actuals(
                aep, kpi_matrix, period, label, description)
        elif period.source == SRC_ACTUALS_ALT:
            return self._add_column_actuals_alt(
                aep, kpi_matrix, period, label, description)
        elif period.source == SRC_SUMCOL:
            return self._add_column_sumcol(
                aep, kpi_matrix, period, label, description)
        elif period.source == SRC_CMPCOL:
            return self._add_column_cmpcol(
                aep, kpi_matrix, period, label, description)

    @api.multi
    def _compute_matrix(self):
        """ Compute a report and return a KpiMatrix.

        The key attribute of the matrix columns (KpiMatrixCol)
        is guaranteed to be the id of the mis.report.instance.period.
        """
        self.ensure_one()
        currency = self.currency_id
        if not self.currency_id:
            currency = self.env['res.company']._get_user_currency()
        aep = self.report_id._prepare_aep(self.company_ids, currency,
                                         self.exchange_rate_date)
        kpi_matrix = self.report_id.prepare_kpi_matrix()
        for period in self.period_ids:
            description = None
            if period.mode == MODE_NONE:
                pass
            elif period.date_from == period.date_to and period.date_from:
                description = self._format_date(period.date_from)
            elif period.date_from and period.date_to:
                date_from = self._format_date(period.date_from)
                date_to = self._format_date(period.date_to)
                description = _('from %s to %s') % (date_from, date_to)
            self._add_column(aep, kpi_matrix, period, period.name, description)
        kpi_matrix.compute_comparisons()
        kpi_matrix.compute_sums()
        return kpi_matrix

    @api.multi
    def compute(self):
        self.ensure_one()
        kpi_matrix = self._compute_matrix()
        return kpi_matrix.as_dict()

    @api.multi
    def drilldown(self, arg):
        self.ensure_one()
        currency = self.currency_id
        if not self.currency_id:
            currency = self.env['res.company']._get_user_currency()
        period_id = arg.get('period_id')
        expr = arg.get('expr')
        account_id = arg.get('account_id')
        if period_id and expr and AEP.has_account_var(expr):
            period = self.env['mis.report.instance.period'].browse(period_id)
            aep = AEP(self.company_ids, currency, self.exchange_rate_date)
            aep.parse_expr(expr)
            aep.done_parsing()
            domain = aep.get_aml_domain_for_expr(
                expr,
                period.date_from, period.date_to,
                self.target_move if period.source == SRC_ACTUALS else None,
                account_id)
            domain.extend(period._get_additional_move_line_filter())
            if period.source == SRC_ACTUALS_ALT:
                aml_model_name = period.source_aml_model_id.model
            else:
                aml_model_name = 'account.move.line'
            return {
                'name': u'{} - {}'.format(expr, period.name),
                'domain': domain,
                'type': 'ir.actions.act_window',
                'res_model': aml_model_name,
                'views': [[False, 'list'], [False, 'form']],
                'view_type': 'list',
                'view_mode': 'list',
                'target': 'current',
            }
        else:
            return False
