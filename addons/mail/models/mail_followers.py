# -*- coding: utf-8 -*-

import threading

from openerp import _, api, fields, models
from openerp import tools


class Followers(models.Model):
    """ mail_followers holds the data related to the follow mechanism inside
    Odoo. Partners can choose to follow documents (records) of any kind
    that inherits from mail.thread. Following documents allow to receive
    notifications for new messages. A subscription is characterized by:

    :param: res_model: model of the followed objects
    :param: res_id: ID of resource (may be 0 for every objects)
    """
    _name = 'mail.followers'
    _rec_name = 'partner_id'
    _log_access = False
    _description = 'Document Followers'

    res_model = fields.Char(
        'Related Document Model', required=True, select=1, help='Model of the followed resource')
    res_id = fields.Integer(
        'Related Document ID', select=1, help='Id of the followed resource')
    partner_id = fields.Many2one(
        'res.partner', string='Related Partner', ondelete='cascade', required=True, select=1)
    subtype_ids = fields.Many2many(
        'mail.message.subtype', string='Subtype',
        help="Message subtypes followed, meaning subtypes that will be pushed onto the user's Wall.")

    #
    # Modifying followers change access rights to individual documents. As the
    # cache may contain accessible/inaccessible data, one has to refresh it.
    #
    @api.model
    def create(self, vals):
        res = super(Followers, self).create(vals)
        self.invalidate_cache()
        return res

    @api.multi
    def write(self, vals):
        res = super(Followers, self).write(vals)
        self.invalidate_cache()
        return res

    @api.multi
    def unlink(self):
        res = super(Followers, self).unlink()
        self.invalidate_cache()
        return res

    _sql_constraints = [('mail_followers_res_partner_res_model_id_uniq', 'unique(res_model,res_id,partner_id)', 'Error, a partner cannot follow twice the same object.')]


class Notification(models.Model):
    """ Class holding notifications pushed to partners. Followers and partners
    added in 'contacts to notify' receive notifications. """
    _name = 'mail.notification'
    _rec_name = 'partner_id'
    _log_access = False
    _description = 'Notifications'

    partner_id = fields.Many2one('res.partner', string='Contact', ondelete='cascade', required=True, select=1)
    is_read = fields.Boolean('Read', select=1, oldname='read')
    starred = fields.Boolean('Starred', select=1, help='Starred message that goes into the todo mailbox')
    message_id = fields.Many2one('mail.message', string='Message', ondelete='cascade', required=True, select=1)

    def init(self, cr):
        cr.execute('SELECT indexname FROM pg_indexes WHERE indexname = %s', ('mail_notification_partner_id_read_starred_message_id',))
        if not cr.fetchone():
            cr.execute('CREATE INDEX mail_notification_partner_id_read_starred_message_id ON mail_notification (partner_id, is_read, starred, message_id)')

    def get_partners_to_email(self, message):
        """ Return the list of partners to notify, based on their preferences.

            :param browse_record message: mail.message to notify
            :param list partners_to_notify: optional list of partner ids restricting
                the notifications to process
        """
        notify_pids = []
        for notification in self:
            if notification.is_read:
                continue
            partner = notification.partner_id
            # Do not send to partners without email address defined
            if not partner.email:
                continue
            # Do not send to partners having same email address than the author (can cause loops or bounce effect due to messy database)
            if message.author_id and message.author_id.email == partner.email:
                continue
            # Partner does not want to receive any emails or is opt-out
            if partner.notify_email == 'none':
                continue
            notify_pids.append(partner.id)
        return notify_pids

    @api.model
    def get_signature_footer(self, user_id, res_model=None, res_id=None, user_signature=True):
        """ Format a standard footer for notification emails (such as pushed messages
            notification or invite emails).
            Format:
                <p>--<br />
                    Administrator
                </p>
                <div>
                    <small>Sent from <a ...>Your Company</a> using <a ...>Odoo</a>.</small>
                </div>
        """
        footer = ""
        if not user_id:
            return footer

        # add user signature
        user = self.env.user
        if user_signature:
            if self.env.user.signature:
                signature = user.signature
            else:
                signature = "--<br />%s" % user.name
            footer = tools.append_content_to_html(footer, signature, plaintext=False)

        # add company signature
        if user.company_id.website:
            website_url = ('http://%s' % user.company_id.website) if not user.company_id.website.lower().startswith(('http:', 'https:')) \
                else user.company_id.website
            company = "<a style='color:inherit' href='%s'>%s</a>" % (website_url, user.company_id.name)
        else:
            company = user.company_id.name
        sent_by = _('Sent by %(company)s using %(odoo)s')

        signature_company = '<br /><small>%s</small>' % (sent_by % {
            'company': company,
            'odoo': "<a style='color:inherit' href='https://www.odoo.com/'>Odoo</a>"
        })
        footer = tools.append_content_to_html(footer, signature_company, plaintext=False, container_tag='div')

        return footer

    def update_message_notification(self, message, partners):
        # update existing notifications
        self.write({'is_read': False})

        # create new notifications
        new_notif_ids = self.env['mail.notification']
        for new_pid in partners - self.mapped('partner_id'):
            new_notif_ids |= self.create({'message_id': message.id, 'partner_id': new_pid.id, 'is_read': False})
        return new_notif_ids

    @api.multi
    def _notify_email(self, message, force_send=False, user_signature=True):

        # compute partners
        email_pids = self.get_partners_to_email(message)
        if not email_pids:
            return True

        # compute email body (signature, company data)
        body_html = message.body
        # add user signature except for mail groups, where users are usually adding their own signatures already
        user_id = message.author_id and message.author_id.user_ids and message.author_id.user_ids[0] and message.author_id.user_ids[0].id or None
        signature_company = self.get_signature_footer(user_id, res_model=message.model, res_id=message.res_id, user_signature=(user_signature and message.model != 'mail.group'))
        if signature_company:
            body_html = tools.append_content_to_html(body_html, signature_company, plaintext=False, container_tag='div')

        # compute email references
        references = message.parent_id.message_id if message.parent_id else False

        # custom values
        custom_values = dict()
        if message.model and message.res_id and self.pool.get(message.model) and hasattr(self.pool[message.model], 'message_get_email_values'):
            custom_values = self.env[message.model].browse(message.res_id).message_get_email_values(message)

        # create email values
        max_recipients = 50
        chunks = [email_pids[x:x + max_recipients] for x in xrange(0, len(email_pids), max_recipients)]
        emails = self.env['mail.mail']
        for chunk in chunks:
            mail_values = {
                'mail_message_id': message.id,
                'auto_delete': self._context.get('mail_auto_delete', True),
                'mail_server_id': self._context.get('mail_server_id', False),
                'body_html': body_html,
                'recipient_ids': [(4, id) for id in chunk],
                'references': references,
            }
            mail_values.update(custom_values)
            emails |= self.env['mail.mail'].create(mail_values)
        # NOTE:
        #   1. for more than 50 followers, use the queue system
        #   2. do not send emails immediately if the registry is not loaded,
        #      to prevent sending email during a simple update of the database
        #      using the command-line.
        if force_send and len(chunks) < 2 and \
               (not self.pool._init or
                getattr(threading.currentThread(), 'testing', False)):
            emails.send()
        return True

    @api.model
    def _notify(self, message, recipients=None, force_send=False, user_signature=True):
        """ Send by email the notification depending on the user preferences

            :param list partners_to_notify: optional list of partner ids restricting
                the notifications to process
            :param bool force_send: if True, the generated mail.mail is
                immediately sent after being created, as if the scheduler
                was executed for this message only.
            :param bool user_signature: if True, the generated mail.mail body is
                the body of the related mail.message with the author's signature
        """
        notif_ids = self.sudo().search([('message_id', '=', message.id), ('partner_id', 'in', recipients.ids)])

        # update or create notifications
        new_notif_ids = notif_ids.update_message_notification(message, recipients)  # tde check: sudo

        # mail_notify_noemail (do not send email) or no partner_ids: do not send, return
        if self.env.context.get('mail_notify_noemail'):
            return True

        # browse as SUPERUSER_ID because of access to res_partner not necessarily allowed
        new_notif_ids._notify_email(message, force_send, user_signature)  # tde check this one too