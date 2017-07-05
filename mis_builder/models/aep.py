# -*- coding: utf-8 -*-
# © 2014-2015 ACSONE SA/NV (<http://acsone.eu>)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

import re
from collections import defaultdict
from itertools import izip

from odoo import fields, _
from odoo.models import expression
from odoo.exceptions import UserError
from odoo.tools.safe_eval import safe_eval
from odoo.tools.float_utils import float_is_zero
from .accounting_none import AccountingNone


class AccountingExpressionProcessor(object):
    """ Processor for accounting expressions.

    Expressions of the form <field><mode>[accounts][optional move line domain]
    are supported, where:
        * field is bal, crd, deb
        * mode is i (initial balance), e (ending balance),
          p (moves over period)
        * there is also a special u mode (unallocated P&L) which computes
          the sum from the beginning until the beginning of the fiscal year
          of the period; it is only meaningful for P&L accounts
        * accounts is a list of accounts, possibly containing % wildcards
        * an optional domain on move lines allowing filters on eg analytic
          accounts or journal
        * an optional parameter for rate calculation. this parameter will
          override the default value set in the report instance.
          possible values are
          empty or n: current date exchange ratenow,
          s: start date of period (date_from)
          e: end date of period (date_to)
          d: daily rate of move line date.

    Examples:
        * bal[70]: variation of the balance of moves on account 70
          over the period (it is the same as balp[70]);
        * bali[70,60]: balance of accounts 70 and 60 at the start of period;
        * bale[1%]: balance of accounts starting with 1 at end of period.

    How to use:
        * repeatedly invoke parse_expr() for each expression containing
          accounting variables as described above; this lets the processor
          group domains and modes and accounts;
        * when all expressions have been parsed, invoke done_parsing()
          to notify the processor that it can prepare to query (mainly
          search all accounts - children, consolidation - that will need to
          be queried;
        * for each period, call do_queries(), then call replace_expr() for each
          expression to replace accounting variables with their resulting value
          for the given period.

    How it works:
        * by accumulating the expressions before hand, it ensures to do the
          strict minimum number of queries to the database (for each period,
          one query per domain and mode);
        * it queries using the orm read_group which reduces to a query with
          sum on debit and credit and group by on account_id (note: it seems
          the orm then does one query per account to fetch the account
          name...);
        * additionally, one query per view/consolidation account is done to
          discover the children accounts.
    """
    MODE_VARIATION = 'p'
    MODE_INITIAL = 'i'
    MODE_END = 'e'
    MODE_UNALLOCATED = 'u'

    _ACC_RE = re.compile(r"(?P<field>\bbal|\bcrd|\bdeb)"
                         r"(?P<mode>[piseu])?"
                         r"(?P<accounts>_[a-zA-Z0-9]+|\[.*?\])"
                         r"(?P<domain>\[.*?\])?"
                         r"(?P<exchange_rate_date>[send])?")

    def __init__(self, companies, currency=None, exchange_rate_date='n'):
        self.companies = companies
        if not currency:
            self.currency = companies.mapped('currency_id')
            if len(self.currency) > 1:
                raise UserError(_('"If currency_id is not given, \
                    every companies must have the same currency."'))
        else:
            self.currency = currency
        # exchange_rate_date can be 
        # n (now), s (date_from), e (date_to), d (daily)
        self.exchange_rate_date = exchange_rate_date
        self.dp = self.currency.decimal_places
        # before done_parsing: {(domain, mode): set(account_codes)}
        # after done_parsing: {(domain, mode): list(account_ids)}
        self._map_account_ids = defaultdict(set)
        # {account_code: account_id} where account_code can be
        # - None for all accounts
        # - NNN% for a like
        # - NNN for a code with an exact match
        self._account_ids_by_code = defaultdict(set)
        # smart ending balance (returns AccountingNone if there
        # are no moves in period and 0 initial balance), implies
        # a first query to get the initial balance and another
        # to get the variation, so it's a bit slower
        self.smart_end = True

    def _load_account_codes(self, account_codes):
        account_model = self.companies.env['account.account']
        exact_codes = set()
        for account_code in account_codes:
            if account_code in self._account_ids_by_code:
                continue
            if account_code is None:
                # None means we want all accounts
                account_ids = account_model.\
                    search([('company_id', 'in', self.companies.ids)]).ids
                self._account_ids_by_code[account_code].update(account_ids)
            elif '%' in account_code:
                account_ids = account_model.\
                    search([('code', '=like', account_code),
                            ('company_id', 'in', self.companies.ids)]).ids
                self._account_ids_by_code[account_code].update(account_ids)
            else:
                # search exact codes after the loop to do less queries
                exact_codes.add(account_code)
        for account in account_model.\
                search([('code', 'in', list(exact_codes)),
                        ('company_id', 'in', self.companies.ids)]):
            self._account_ids_by_code[account.code].add(account.id)

    def _parse_match_object(self, mo):
        """Split a match object corresponding to an accounting variable

        Returns field, mode, [account codes], (domain expression).
        """
        field, mode, account_codes, domain, exchange_rate_date = mo.groups()
        if not mode:
            mode = self.MODE_VARIATION
        elif mode == 's':
            mode = self.MODE_END
        if account_codes.startswith('_'):
            account_codes = account_codes[1:]
        else:
            account_codes = account_codes[1:-1]
        if account_codes.strip():
            account_codes = [a.strip() for a in account_codes.split(',')]
        else:
            account_codes = [None]  # None means we want all accounts
        domain = domain or '[]'
        domain = tuple(safe_eval(domain))
        return field, mode, account_codes, domain, exchange_rate_date

    def parse_expr(self, expr):
        """Parse an expression, extracting accounting variables.

        Domains and accounts are extracted and stored in the map
        so when all expressions have been parsed, we know which
        account codes to query for each domain and mode.
        """
        for mo in self._ACC_RE.finditer(expr):
            _, mode, account_codes, domain, exchange_rate_date = \
                self._parse_match_object(mo)
            if mode == self.MODE_END and self.smart_end:
                modes = (self.MODE_INITIAL, self.MODE_VARIATION, self.MODE_END)
            else:
                modes = (mode, )
            for mode in modes:
                key = (domain, mode, exchange_rate_date)
                self._map_account_ids[key].update(account_codes)

    def done_parsing(self):
        """Load account codes and replace account codes by
        account ids in map."""
        for key, account_codes in self._map_account_ids.items():
            # TODO _load_account_codes could be done
            # for all account_codes at once (also in v8)
            self._load_account_codes(account_codes)
            account_ids = set()
            for account_code in account_codes:
                account_ids.update(self._account_ids_by_code[account_code])
            self._map_account_ids[key] = list(account_ids)

    @classmethod
    def has_account_var(cls, expr):
        """Test if an string contains an accounting variable."""
        return bool(cls._ACC_RE.search(expr))

    def get_account_ids_for_expr(self, expr):
        """ Get a set of account ids that are involved in an expression.

        Prerequisite: done_parsing() must have been invoked.
        """
        account_ids = set()
        for mo in self._ACC_RE.finditer(expr):
            field, mode, account_codes, domain, exchange_rate_date = \
                self._parse_match_object(mo)
            for account_code in account_codes:
                account_ids.update(self._account_ids_by_code[account_code])
        return account_ids

    def get_aml_domain_for_expr(self, expr,
                                date_from, date_to,
                                target_move,
                                account_id=None):
        """ Get a domain on account.move.line for an expression.

        Prerequisite: done_parsing() must have been invoked.

        Returns a domain that can be used to search on account.move.line.
        """
        aml_domains = []
        date_domain_by_mode = {}
        for mo in self._ACC_RE.finditer(expr):
            field, mode, account_codes, domain, exchange_rate_date = \
                self._parse_match_object(mo)
            aml_domain = list(domain)
            account_ids = set()
            for account_code in account_codes:
                account_ids.update(self._account_ids_by_code[account_code])
            if not account_id:
                aml_domain.append(('account_id', 'in', tuple(account_ids)))
            else:
                # filter on account_id
                if account_id in account_ids:
                    aml_domain.append(('account_id', '=', account_id))
                else:
                    continue
            if field == 'crd':
                aml_domain.append(('credit', '>', 0))
            elif field == 'deb':
                aml_domain.append(('debit', '>', 0))
            aml_domains.append(expression.normalize_domain(aml_domain))
            if mode not in date_domain_by_mode:
                date_domain_by_mode[mode] = \
                    self.get_aml_domain_for_dates(date_from, date_to,
                                                  mode, target_move)
        assert aml_domains
        return expression.OR(aml_domains) + \
            expression.OR(date_domain_by_mode.values())

    def get_aml_domain_for_dates(self, date_from, date_to,
                                 mode,
                                 target_move):
        if mode == self.MODE_VARIATION:
            domain = [('date', '>=', date_from), ('date', '<=', date_to)]
        elif mode in (self.MODE_INITIAL, self.MODE_END):
            # for income and expense account, sum from the beginning
            # of the current fiscal year only, for balance sheet accounts
            # sum from the beginning of time
            date_from_date = fields.Date.from_string(date_from)
            fy_date_from = \
                self.companies.\
                compute_fiscalyear_dates(date_from_date)['date_from']
            domain = ['|',
                      ('date', '>=', fields.Date.to_string(fy_date_from)),
                      ('user_type_id.include_initial_balance', '=', True)]
            if mode == self.MODE_INITIAL:
                domain.append(('date', '<', date_from))
            elif mode == self.MODE_END:
                domain.append(('date', '<=', date_to))
        elif mode == self.MODE_UNALLOCATED:
            date_from_date = fields.Date.from_string(date_from)
            fy_date_from = \
                self.companies.\
                compute_fiscalyear_dates(date_from_date)['date_from']
            domain = [('date', '<', fields.Date.to_string(fy_date_from)),
                      ('user_type_id.include_initial_balance', '=', False)]
        if target_move == 'posted':
            domain.append(('move_id.state', '=', 'posted'))
        return expression.normalize_domain(domain)

    def get_company_rates(self, date=None):
        # get exchange rates for each company with its rouding
        company_rates = {}
        cur_model = self.companies.env['res.currency']
        used_currency_dated = \
        cur_model.with_context(date=date).browse(self.currency.id)
        for company in self.companies:
            if company.currency_id != used_currency_dated:
                company_currency_dated =\
                    cur_model.with_context(
                        date=date).browse(company.currency_id.id)
                rate = used_currency_dated.rate / company_currency_dated.rate
            else:
                rate = 1.0
            company_rates[company.id] = (rate,
                                         company.currency_id.decimal_places)
        return company_rates

    def do_queries(self, date_from, date_to,
                   target_move='posted', additional_move_line_filter=None,
                   aml_model=None):
        """Query sums of debit and credit for all accounts and domains
        used in expressions.

        This method must be executed after done_parsing().
        """
        if not aml_model:
            aml_model = self.companies.env['account.move.line']
        else:
            aml_model = self.companies.env[aml_model]
        cur_model = self.companies.env['res.currency']
        if not self.exchange_rate_date or self.exchange_rate_date == 'n':
            company_rates = self.get_company_rates()
        elif self.exchange_rate_date == 'e':
            company_rates = self.get_company_rates(date_to)
        elif self.exchange_rate_date == 's':
            company_rates = self.get_company_rates(date_from)
        # {(domain, mode): {account_id: (debit, credit)}}
        self._data = defaultdict(dict)
        domain_by_mode = {}
        ends = []
        for key in self._map_account_ids:
            domain, mode, ex_rate_date = key
            exchange_rate_date = ex_rate_date
            if not exchange_rate_date or \
                    exchange_rate_date == self.exchange_rate_date:
                exchange_rate_date = self.exchange_rate_date
            elif exchange_rate_date != 'd':
                if exchange_rate_date == 'n':
                    company_rates = self.get_company_rates()
                elif exchange_rate_date == 'e':
                    company_rates = self.get_company_rates(date_to)
                elif exchange_rate_date == 's':
                    company_rates = self.get_company_rates(date_from)
            if mode == self.MODE_END and self.smart_end:
                # postpone computation of ending balance
                ends.append((domain, mode, ex_rate_date))
                continue
            if mode not in domain_by_mode:
                domain_by_mode[mode] = \
                    self.get_aml_domain_for_dates(date_from, date_to,
                                                  mode, target_move)
            domain = list(domain) + domain_by_mode[mode]
            domain.append(('account_id', 'in', self._map_account_ids[key]))
            if additional_move_line_filter:
                domain.extend(additional_move_line_filter)
            # fetch sum of debit/credit, grouped by account_id
            if self.exchange_rate_date == 'd':
                query = aml_model._where_calc(domain)
                from_clause, where_clause, where_clause_params = \
                    query.get_sql()
                where_str = where_clause and (" WHERE %s" % where_clause) or ''
                query_str = """
                    SELECT
                        SUM(debit),
                        SUM(credit),
                        account_id,
                        company_currency_id,
                        date_maturity FROM """ + from_clause + where_str +\
                    """ GROUP BY
                        account_id,
                        company_currency_id,
                        date_maturity"""
                aml_model._cr.execute(query_str, where_clause_params)
                res = aml_model._cr.fetchall()
                for acc in res:
                    date = acc[4]
                    company_currency_id = acc[3]
                    debit = acc[0] or 0.0
                    credit = acc[1] or 0.0
                    company_currency_dated = cur_model.with_context(date=date).\
                        browse(company_currency_id)
                    dp = company_currency_dated.decimal_places
                    if mode in (self.MODE_INITIAL, self.MODE_UNALLOCATED) and \
                            float_is_zero(debit-credit,
                                          precision_rounding=dp):
                        # in initial mode, ignore accounts with 0 balance
                        continue
                    used_currency_dated = \
                        cur_model.with_context(date=date).\
                            browse(self.currency)
                    rate = used_currency_dated.rate / company_currency_dated.rate
                    if not self._data[key].get(acc[2]):
                        self._data[key][acc[2]] = (0, 0)
                    acc_debit = self._data[key][acc[2]][0]
                    acc_credit = self._data[key][acc[2]][1]
                    self._data[key][acc[2]] =\
                        (acc_debit+debit*rate, acc_credit+credit*rate)
            else:
                accs = aml_model.read_group(
                    domain,
                    ['debit', 'credit', 'account_id', 'company_id'],
                    ['account_id', 'company_id'], lazy=False)
                for acc in accs:
                    rate, dp = company_rates[acc['company_id'][0]]
                    debit = acc['debit'] or 0.0
                    credit = acc['credit'] or 0.0
                    if mode in (self.MODE_INITIAL, self.MODE_UNALLOCATED) and \
                            float_is_zero(debit-credit,
                                          precision_rounding=dp):
                        # in initial mode, ignore accounts with 0 balance
                        continue
                    self._data[key][acc['account_id'][0]] =\
                        (debit*rate, credit*rate)
        # compute ending balances by summing initial and variation
        for key in ends:
            domain, mode, exchange_rate_date = key
            initial_data = self._data[(domain,
                                       self.MODE_INITIAL, exchange_rate_date)]
            variation_data = self._data[(domain,
                                         self.MODE_VARIATION, exchange_rate_date)]
            account_ids = set(initial_data.keys()) | set(variation_data.keys())
            for account_id in account_ids:
                di, ci = initial_data.get(account_id,
                                          (AccountingNone, AccountingNone))
                dv, cv = variation_data.get(account_id,
                                            (AccountingNone, AccountingNone))
                self._data[key][account_id] = (di + dv, ci + cv)

    def replace_expr(self, expr):
        """Replace accounting variables in an expression by their amount.

        Returns a new expression string.

        This method must be executed after do_queries().
        """
        def f(mo):
            field, mode, account_codes, domain, exchange_rate_date = \
                self._parse_match_object(mo)
            key = (domain, mode, exchange_rate_date)
            account_ids_data = self._data[key]
            v = AccountingNone
            for account_code in account_codes:
                account_ids = self._account_ids_by_code[account_code]
                for account_id in account_ids:
                    debit, credit = \
                        account_ids_data.get(account_id,
                                             (AccountingNone, AccountingNone))
                    if field == 'bal':
                        v += debit - credit
                    elif field == 'deb':
                        v += debit
                    elif field == 'crd':
                        v += credit
            # in initial balance mode, assume 0 is None
            # as it does not make sense to distinguish 0 from "no data"
            if v is not AccountingNone and \
                    mode in (self.MODE_INITIAL, self.MODE_UNALLOCATED) and \
                    float_is_zero(v, precision_rounding=self.dp):
                v = AccountingNone
            return '(' + repr(v) + ')'

        return self._ACC_RE.sub(f, expr)

    def replace_exprs_by_account_id(self, exprs):
        """Replace accounting variables in a list of expression
        by their amount, iterating by accounts involved in the expression.

        yields account_id, replaced_expr

        This method must be executed after do_queries().
        """
        def f(mo):
            field, mode, account_codes, domain, exchange_rate_date = \
                self._parse_match_object(mo)
            key = (domain, mode, exchange_rate_date)
            # first check if account_id is involved in
            # the current expression part
            found = False
            for account_code in account_codes:
                if account_id in self._account_ids_by_code[account_code]:
                    found = True
                    break
            if not found:
                return '(AccountingNone)'
            # here we know account_id is involved in account_codes
            account_ids_data = self._data[key]
            debit, credit = \
                account_ids_data.get(account_id,
                                     (AccountingNone, AccountingNone))
            if field == 'bal':
                v = debit - credit
            elif field == 'deb':
                v = debit
            elif field == 'crd':
                v = credit
            # in initial balance mode, assume 0 is None
            # as it does not make sense to distinguish 0 from "no data"
            if v is not AccountingNone and \
                    mode in (self.MODE_INITIAL, self.MODE_UNALLOCATED) and \
                    float_is_zero(v, precision_rounding=self.dp):
                v = AccountingNone
            return '(' + repr(v) + ')'

        account_ids = set()
        for expr in exprs:
            for mo in self._ACC_RE.finditer(expr):
                field, mode, account_codes, domain, exchange_rate_date = \
                    self._parse_match_object(mo)
                key = (domain, mode, exchange_rate_date)
                account_ids_data = self._data[key]
                for account_code in account_codes:
                    for account_id in self._account_ids_by_code[account_code]:
                        if account_id in account_ids_data:
                            account_ids.add(account_id)

        for account_id in account_ids:
            yield account_id, [self._ACC_RE.sub(f, expr) for expr in exprs]

    @classmethod
    def _get_balances(cls, mode, companies, date_from, date_to,
                      target_move='posted'):
        expr = 'deb{mode}[], crd{mode}[]'.format(mode=mode)
        aep = AccountingExpressionProcessor(companies)
        # disable smart_end to have the data at once, instead
        # of initial + variation
        aep.smart_end = False
        aep.parse_expr(expr)
        aep.done_parsing()
        aep.do_queries(date_from, date_to, target_move)
        return aep._data[((), mode)]

    @classmethod
    def get_balances_initial(cls, companies, date, target_move='posted'):
        """ A convenience method to obtain the initial balances of all accounts
        at a given date.

        It is the same as get_balances_end(date-1).

        :param companies:
        :param date:
        :param target_move: if 'posted', consider only posted moves

        Returns a dictionary: {account_id, (debit, credit)}
        """
        return cls._get_balances(cls.MODE_INITIAL, companies,
                                 date, date, target_move)

    @classmethod
    def get_balances_end(cls, companies, date, target_move='posted'):
        """ A convenience method to obtain the ending balances of all accounts
        at a given date.

        It is the same as get_balances_initial(date+1).

        :param companies:
        :param date:
        :param target_move: if 'posted', consider only posted moves

        Returns a dictionary: {account_id, (debit, credit)}
        """
        return cls._get_balances(cls.MODE_END, companies,
                                 date, date, target_move)

    @classmethod
    def get_balances_variation(cls, companies, date_from, date_to,
                               target_move='posted'):
        """ A convenience method to obtain the variation of the
        balances of all accounts over a period.

        :param companies:
        :param date:
        :param target_move: if 'posted', consider only posted moves

        Returns a dictionary: {account_id, (debit, credit)}
        """
        return cls._get_balances(cls.MODE_VARIATION, companies,
                                 date_from, date_to, target_move)

    @classmethod
    def get_unallocated_pl(cls, companies, date, target_move='posted'):
        """ A convenience method to obtain the unallocated profit/loss
        of the previous fiscal years at a given date.

        :param companies:
        :param date:
        :param target_move: if 'posted', consider only posted moves

        Returns a tuple (debit, credit)
        """
        # TODO shoud we include here the accounts of type "unaffected"
        # or leave that to the caller?
        bals = cls._get_balances(cls.MODE_UNALLOCATED, companies,
                                 date, date, target_move)
        return tuple(map(sum, izip(*bals.values())))
