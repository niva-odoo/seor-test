# Copyright 2009-2021 Noviat.
# License LGPL-3 or later (http://www.gnu.org/licenses/lpgl).

import base64
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class EbicsFile(models.Model):
    _name = 'ebics.file'
    _description = 'Object to store EBICS Data Files'
    _order = 'date desc'
    _sql_constraints = [
        ('name_uniq', 'unique (name, format_id)',
         'This File has already been down- or uploaded !')
    ]

    name = fields.Char(string='Filename')
    data = fields.Binary(string='File', readonly=True)
    format_id = fields.Many2one(
        comodel_name='ebics.file.format',
        string='EBICS File Formats',
        readonly=True)
    type = fields.Selection(
        related='format_id.type',
        readonly=True)
    date_from = fields.Date(
        readonly=True,
        help="'Date From' as entered in the download wizard.")
    date_to = fields.Date(
        readonly=True,
        help="'Date To' as entered in the download wizard.")
    date = fields.Datetime(
        required=True, readonly=True,
        help='File Upload/Download date')
    bank_statement_ids = fields.One2many(
        comodel_name='account.bank.statement',
        inverse_name='ebics_file_id',
        string='Generated Bank Statements', readonly=True)
    state = fields.Selection(
        [('draft', 'Draft'),
         ('done', 'Done')],
        string='State',
        default='draft',
        required=True, readonly=True)
    user_id = fields.Many2one(
        comodel_name='res.users', string='User',
        default=lambda self: self.env.user,
        readonly=True)
    ebics_userid_id = fields.Many2one(
        comodel_name='ebics.userid',
        string='EBICS UserID',
        ondelete='restrict',
        readonly=True)
    note = fields.Text(string='Notes')
    note_process = fields.Text(string='Notes')
    company_ids = fields.Many2many(
        comodel_name='res.company',
        string='Companies',
        help="Companies sharing this EBICS file.")

    def unlink(self):
        ff_methods = self._file_format_methods()
        for ebics_file in self:
            if ebics_file.state == 'done':
                raise UserError(_(
                    "You can only remove EBICS files in state 'Draft'."))
            # execute format specific actions
            ff = ebics_file.format_id.download_process_method
            if ff in ff_methods:
                if ff_methods[ff].get('unlink'):
                    ff_methods[ff]['unlink'](ebics_file)
            # remove bank statements
            ebics_file.bank_statement_ids.unlink()
        return super(EbicsFile, self).unlink()

    def set_to_draft(self):
        return self.write({'state': 'draft'})

    def set_to_done(self):
        return self.write({'state': 'done'})

    def process(self):
        self.ensure_one()
        ctx = dict(
            self.env.context,
            allowed_company_ids=self.env.user.company_ids.ids)
        self = self.with_context(ctx)
        self.note_process = ''
        ff_methods = self._file_format_methods()
        ff = self.format_id.download_process_method
        if ff in ff_methods:
            if ff_methods[ff].get('process'):
                res = ff_methods[ff]['process'](self)
                self.state = 'done'
                return res
        else:
            return self._process_undefined_format()

    def action_open_bank_statements(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'account.action_bank_statement_tree')
        domain = eval(action.get('domain') or '[]')
        domain += [('id', 'in', self._context.get('statement_ids'))]
        action.update({'domain': domain})
        return action

    def button_close(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window_close'}

    def _file_format_methods(self):
        """
        Extend this dictionary in order to add support
        for extra file formats.
        """
        res = {
            'cfonb120':
                {'process': self._process_cfonb120,
                 'unlink': self._unlink_cfonb120},
            'camt.052':
                {'process': self._process_camt052,
                 'unlink': self._unlink_camt052},
            'camt.053':
                {'process': self._process_camt053,
                 'unlink': self._unlink_camt053},
            'camt.054':
                {'process': self._process_camt054,
                 'unlink': self._unlink_camt054},
            'pain.002':
                {'process': self._process_pain002,
                 'unlink': self._unlink_pain002},
        }
        return res

    def _check_import_module(self, module, raise_if_not_found=True):
        mod = self.env['ir.module.module'].sudo().search(
            [('name', '=like', module),
             ('state', '=', 'installed')])
        if not mod:
            if raise_if_not_found:
                raise UserError(_(
                    "The module to process the '%s' format is not installed "
                    "on your system. "
                    "\nPlease install module '%s'")
                    % (self.format_id.name, module))
            return False
        return True

    def _process_result_action(self, res):
        notifications = []
        st_line_ids = []
        statement_ids = []
        if res.get('context'):
            notifications = res['context'].get('notifications', [])
            st_line_ids = res['context'].get('statement_line_ids', [])
        if notifications:
            for notif in notifications:
                parts = []
                for k in ['type', 'message', 'details']:
                    if notif.get(k):
                        msg = '%s: %s' % (k, notif[k])
                        parts.append(msg)
                self.note_process += '\n'.join(parts)
                self.note_process += '\n'
            self.note_process += '\n'
        if st_line_ids:
            self.flush()
            self.env.cr.execute(
                """
    SELECT DISTINCT
        absl.statement_id,
        abs.name, abs.date, abs.company_id,
        rc.name AS company_name
      FROM account_bank_statement_line absl
      INNER JOIN account_bank_statement abs
        ON abs.id = absl.statement_id
      INNER JOIN res_company rc
        ON rc.id = abs.company_id
      WHERE absl.id IN %s
      ORDER BY date, company_id
                """,
                (tuple(st_line_ids),)
            )
            sts_data = self.env.cr.dictfetchall()
        else:
            sts_data = []
        st_cnt = len(sts_data)
        if st_cnt:
            self.note_process += _(
                "%s bank statements have been imported: "
            ) % st_cnt
            self.note_process += '\n'
        for st_data in sts_data:
            self.note_process += ("\n%s, %s (%s)") % (
                st_data['date'], st_data['name'], st_data['company_name'])
        statement_ids = [x['statement_id'] for x in sts_data]
        if statement_ids:
            self.sudo().bank_statement_ids = [(6, 0, statement_ids)]
        ctx = dict(self.env.context, statement_ids=statement_ids)
        module = __name__.split('addons.')[1].split('.')[0]
        result_view = self.env.ref('%s.ebics_file_view_form_result' % module)
        return {
            'name': _('Import EBICS File'),
            'res_id': self.id,
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': self._name,
            'view_id': result_view.id,
            'target': 'new',
            'context': ctx,
            'type': 'ir.actions.act_window',
        }

    @staticmethod
    def _process_cfonb120(self):
        """
        We do not support the standard _journal_creation_wizard since a single
        cfonb120 file may contain statements from different legal entities.
        """
        import_module = 'account_statement_import_fr_cfonb'
        self._check_import_module(import_module)
        wiz_model = 'account.statement.import'
        data_file = base64.b64decode(self.data)
        lines = data_file.split(b'\n')
        wiz_vals_list = []
        st_lines = b''
        transactions = False
        for line in lines:
            rec_type = line[0:2]
            acc_number = line[21:32]
            st_lines += line + b'\n'
            if rec_type == b'04':
                transactions = True
            if rec_type == b'07':
                if transactions:
                    fn = '_'.join([acc_number.decode(), self.name])
                    wiz_vals_list.append({
                        'statement_filename': fn,
                        'statement_file': base64.b64encode(st_lines)
                    })
                st_lines = b''
                transactions = False
        result = {
            'type': 'ir.actions.client',
            'tag': 'bank_statement_reconciliation_view',
            'context': {'statement_line_ids': [],
                        'company_ids': self.env.user.company_ids.ids,
                        'notifications': []},
        }
        wiz_ctx = dict(self.env.context, active_model='ebics.file')
        for i, wiz_vals in enumerate(wiz_vals_list, start=1):
            wiz = self.env[wiz_model].with_context(wiz_ctx).create(wiz_vals)
            res = wiz.import_file_button()
            ctx = res.get('context')
            if (res.get('res_model')
                    == 'account.bank.statement.import.journal.creation'):
                message = _(
                    "Error detected while importing statement number %s.\n"
                ) % i
                message += _("No financial journal found.")
                details = _(
                    'Bank account number: %s'
                ) % ctx.get('default_bank_acc_number')
                result['context']['notifications'].extend([{
                    'type': 'warning',
                    'message': message,
                    'details': details,
                }])
                continue
            result['context']['statement_line_ids'].extend(
                ctx['statement_line_ids'])
            result['context']['notifications'].extend(
                ctx['notifications'])
        return self._process_result_action(result)

    @staticmethod
    def _unlink_cfonb120(self):
        """
        Placeholder for cfonb120 specific actions before removing the
        EBICS data file and its related bank statements.
        """
        pass

    @staticmethod
    def _process_camt052(self):
        import_module = 'account_statement_import_camt'
        self._check_import_module(import_module)
        return self._process_camt053(self)

    @staticmethod
    def _unlink_camt052(self):
        """
        Placeholder for camt052 specific actions before removing the
        EBICS data file and its related bank statements.
        """
        pass

    @staticmethod
    def _process_camt054(self):
        import_module = 'account_statement_import_camt'
        self._check_import_module(import_module)
        return self._process_camt053(self)

    @staticmethod
    def _unlink_camt054(self):
        """
        Placeholder for camt054 specific actions before removing the
        EBICS data file and its related bank statements.
        """
        pass

    @staticmethod
    def _process_camt053(self):
        modules = [
            ('oca', 'account_statement_import_camt'),
            ('oe', 'account_bank_statement_import_camt'),
        ]
        found = False
        for src, mod in modules:
            if self._check_import_module(mod, raise_if_not_found=False):
                found = True
                break
        if not found:
            raise UserError(_(
                "The module to process the '%s' format is not installed "
                "on your system. "
                "\nPlease install one of the following modules: \n%s."
                ) % (self.format_id.name, ', '.join([x[1] for x in modules]))
            )
        if src == 'oca':
            self._process_camt053_oca()
        else:
            self._process_camt053_oe()

    def _process_camt053_oca(self):
        wiz_model = 'account.statement.import'
        wiz_vals = {
            'statement_filename': self.name,
            'statement_file': self.data,
        }
        result = {
            'type': 'ir.actions.client',
            'tag': 'bank_statement_reconciliation_view',
            'context': {'statement_line_ids': [],
                        'company_ids': self.env.user.company_ids.ids,
                        'notifications': []},
        }
        wiz_ctx = dict(self.env.context, active_model='ebics.file')
        wiz = self.env[wiz_model].with_context(wiz_ctx).create(wiz_vals)
        res = wiz.import_file_button()
        ctx = res.get('context')
        if (res.get('res_model')
                == 'account.bank.statement.import.journal.creation'):
            message = _(
                "Error detected while importing statement %s.\n"
            ) % self.name
            message += _("No financial journal found.")
            details = _(
                'Bank account number: %s'
            ) % ctx.get('default_bank_acc_number')
            result['context']['notifications'].extend([{
                'type': 'warning',
                'message': message,
                'details': details,
            }])
        result['context']['statement_line_ids'].extend(
            ctx['statement_line_ids'])
        result['context']['notifications'].extend(
            ctx['notifications'])
        return self._process_result_action(result)

    def _process_camt053_oe(self):
        wiz_model = 'account.bank.statement.import'
        wiz_vals = {
            'attachment_ids': [(0, 0, {'name': self.name,
                                       'datas': self.data,
                                       'store_fname': self.name})]}
        ctx = dict(self.env.context, active_model='ebics.file')
        wiz = self.env[wiz_model].with_context(ctx).create(wiz_vals)
        res = wiz.import_file()
        if res.get('res_model') \
                == 'account.bank.statement.import.journal.creation':
            if res.get('context'):
                bank_account = res['context'].get('default_bank_acc_number')
                raise UserError(_(
                    "No financial journal found for Company Bank Account %s"
                ) % bank_account)
        return self._process_result_action(res)

    @staticmethod
    def _unlink_camt053(self):
        """
        Placeholder for camt053 specific actions before removing the
        EBICS data file and its related bank statements.
        """
        pass

    @staticmethod
    def _process_pain002(self):
        """
        Placeholder for processing pain.002 files.
        TODO:
        add import logic based upon OCA 'account_payment_return_import'
        """
        pass

    @staticmethod
    def _unlink_pain002(self):
        """
        Placeholder for pain.002 specific actions before removing the
        EBICS data file.
        """
        raise NotImplementedError

    def _process_undefined_format(self):
        raise UserError(_(
            "The current version of the 'account_ebics' module "
            "has no support to automatically process EBICS files "
            "with format %s."
        ) % self.format_id.name)
