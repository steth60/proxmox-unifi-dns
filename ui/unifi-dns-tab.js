// UniFi DNS panel for the Proxmox Datacenter view, built from Proxmox's own
// widgets so it follows the selected theme. Data comes from pve-unifi-dns-ui
// on port 8009 of the same node; node DNS and certificates use the Proxmox API
// directly. Installed by pve-unifi-dns; re-applied after pve-manager updates
// by pve-unifi-dns-ui-patch.

Ext.define('PVE.UnifiDNS', {
    singleton: true,

    port: 8009,

    base: function () {
        let host = window.location.hostname;
        if (host.indexOf(':') !== -1) {
            host = '[' + host + ']';
        }
        return 'https://' + host + ':' + this.port;
    },

    iconHtml: function (id, size) {
        if (!id) {
            return '';
        }
        size = size || 24;
        return '<img src="https://static.ui.com/fingerprint/0/' + encodeURIComponent(id) +
            '_257x257.png" style="width:' + size + 'px;height:' + size + 'px;object-fit:contain;' +
            'vertical-align:middle" onerror="this.style.visibility=\'hidden\'">';
    },

    // call the pve-unifi-dns-ui service
    call: function (path, body) {
        let me = this;
        let opts = { credentials: 'include', headers: {} };
        if (body !== undefined) {
            opts.method = 'POST';
            opts.headers['Content-Type'] = 'application/json';
            opts.headers['X-UniFi-DNS'] = '1';
            opts.body = JSON.stringify(body);
        }
        return fetch(me.base() + path, opts).then(
            (r) =>
                r
                    .json()
                    .catch(() => ({}))
                    .then((data) => {
                        if (!r.ok) {
                            throw new Error(data.message || 'HTTP ' + r.status);
                        }
                        return data;
                    }),
            () => {
                throw new Error(
                    gettext('Cannot reach the UniFi DNS service.') + ' ' +
                    Ext.String.format(gettext('Open {0} once and accept the certificate.'), me.base()),
                );
            },
        );
    },

    // call the Proxmox API with the current session
    pve: function (url, method, params) {
        return new Promise((resolve, reject) =>
            Proxmox.Utils.API2Request({
                url: url,
                method: method || 'GET',
                params: params,
                success: (response) => resolve(response.result.data),
                failure: (response) => reject(new Error(response.htmlStatus || response.statusText)),
            }));
    },

    error: function (err) {
        Ext.Msg.alert(gettext('Error'), Ext.String.htmlEncode(err && err.message ? err.message : String(err)));
    },

    ago: function (t) {
        if (!t) {
            return gettext('never');
        }
        let s = Math.max(0, Math.round(Date.now() / 1000 - t));
        if (s < 60) {
            return s + ' s ago';
        }
        if (s < 3600) {
            return Math.round(s / 60) + ' min ago';
        }
        return Math.round(s / 3600) + ' h ago';
    },

    statusHtml: function (good, text) {
        return '<i class="fa fa-fw ' + (good ? 'fa-check good' : 'fa-exclamation-triangle warning') + '"></i> ' +
            Ext.String.htmlEncode(text);
    },
});

Ext.define('pve-unifidns-guest', {
    extend: 'Ext.data.Model',
    fields: ['key', 'kind', 'vmid', 'name', 'unifi_name', 'node', 'status', 'record', 'ip', 'source',
        'notes', 'icon_id', 'icon_title', 'in_unifi', 'sortkey'],
    idProperty: 'key',
});

Ext.define('pve-unifidns-record', {
    extend: 'Ext.data.Model',
    fields: ['name', 'type', 'value', 'owned', 'enabled'],
    idProperty: 'name',
});

// ---------------------------------------------------------------- setup ----

Ext.define('PVE.UnifiDNS.SetupWindow', {
    extend: 'Ext.window.Window',

    title: gettext('UniFi DNS Setup'),
    modal: true,
    width: 780,
    height: 640,
    layout: 'fit',
    border: false,

    onSaved: Ext.emptyFn,

    field: function (name) {
        return this.down('field[name=' + name + ']');
    },

    values: function () {
        return this.form.getForm().getValues();
    },

    fqdn: function () {
        let domain = (this.field('DOMAIN').getValue() || '').trim().toLowerCase().replace(/^\.+|\.+$/g, '');
        return Proxmox.NodeName.toLowerCase() + '.' + (domain || '<domain>');
    },

    updateCertHelp: function () {
        let me = this;
        let fqdn = Ext.String.htmlEncode(me.fqdn());
        me.down('#certHelp').setHtml(
            '<p>' + Ext.String.format(gettext('A trusted certificate for {0} stops the browser warning for Proxmox and this tab.'), '<b>' + fqdn + '</b>') +
            ' ' + gettext('The domain must be one you own, with its DNS at a provider Proxmox supports (Cloudflare and others). Nothing is opened to the internet.') + '</p>' +
            '<ol style="margin:4px 0 0 18px;padding:0">' +
            '<li>' + gettext('Accounts: Add, enter your email and accept the Let\'s Encrypt terms.') + '</li>' +
            '<li>' + gettext('Challenge Plugins: Add, choose your DNS provider and enter its API credentials (Cloudflare: CF_Token).') + '</li>' +
            '<li>' + Ext.String.format(gettext('ACME (further down): Add the domain {0} with challenge type DNS and that plugin, then Order Certificates Now.'), '<b>' + fqdn + '</b>') + '</li>' +
            '</ol><p>' + gettext('Proxmox renews the certificate itself. Save the Domain on the UniFi tab first so the name resolves.') + '</p>');
    },

    load: function () {
        let me = this;
        me.setLoading(true);
        Promise.all([
            PVE.UnifiDNS.call('/api/config'),
            PVE.UnifiDNS.call('/api/node'),
            PVE.UnifiDNS.pve('/nodes/' + Proxmox.NodeName + '/dns'),
        ]).then(
            ([cfg, node, dns]) => {
                if (me.destroyed) {
                    return;
                }
                me.setLoading(false);
                me.cfg = cfg;
                me.form.getForm().setValues(cfg);
                let key = me.field('UNIFI_API_KEY');
                key.allowBlank = !!cfg.UNIFI_API_KEY_SET;
                key.setEmptyText(cfg.UNIFI_API_KEY_SET ? gettext('saved, leave empty to keep it') : gettext('required'));
                me.field('RUN_ON_NODE').setHidden(!cfg.cluster);
                me.fillNode(node, dns);
                me.updateCertHelp();
            },
            (err) => {
                if (!me.destroyed) {
                    me.setLoading(false);
                    PVE.UnifiDNS.error(err);
                }
            },
        );
    },

    fillNode: function (node, dns) {
        let me = this;
        me.nodeState = node;
        me.nodeDns = dns;
        let host = (me.field('UNIFI_HOST').getValue() || '').trim();
        let domain = (me.field('DOMAIN').getValue() || '').trim().toLowerCase();
        let dnsOk = dns.dns1 === host && (dns.search || '') === domain;
        me.down('#nodeDns').setValue(PVE.UnifiDNS.statusHtml(dnsOk,
            Ext.String.format(gettext('server {0}, search domain {1}'), dns.dns1 || '-', dns.search || '-')));
        me.down('#lldp').setValue(PVE.UnifiDNS.statusHtml(node.lldpd.active,
            node.lldpd.installed ? (node.lldpd.active ? gettext('installed and running') : gettext('installed, not running'))
                : gettext('not installed: UniFi shows this node\'s guests beside it, not under it')));
        me.down('#lldpBtn').setDisabled(node.lldpd.installed && node.lldpd.active);
        me.down('#timer').setValue(PVE.UnifiDNS.statusHtml(node.timer.active,
            node.timer.active ? gettext('on, every 2 minutes') : gettext('off')));
        me.down('#timerBtn').setText(node.timer.active ? gettext('Turn Off') : gettext('Turn On'));
    },

    reloadNode: function () {
        let me = this;
        Promise.all([
            PVE.UnifiDNS.call('/api/node'),
            PVE.UnifiDNS.pve('/nodes/' + Proxmox.NodeName + '/dns'),
        ]).then(([node, dns]) => !me.destroyed && me.fillNode(node, dns), PVE.UnifiDNS.error);
    },

    test: function () {
        let me = this;
        let result = me.down('#testResult');
        result.setValue('<i class="fa fa-fw fa-spinner fa-spin"></i> ' + gettext('Testing...'));
        PVE.UnifiDNS.call('/api/test', me.values()).then(
            (r) => {
                if (me.destroyed) {
                    return;
                }
                let text = r.message;
                if (r.ok && r.fingerprint) {
                    me.field('UNIFI_CERT_SHA256').setValue(r.fingerprint);
                    text += '. ' + gettext('Certificate') + ' SHA-256 ' + r.fingerprint.slice(0, 16) + '...';
                }
                result.setValue(PVE.UnifiDNS.statusHtml(r.ok, text));
            },
            (err) => !me.destroyed && result.setValue(PVE.UnifiDNS.statusHtml(false, err.message)),
        );
    },

    save: function () {
        let me = this;
        let form = me.form.getForm();
        if (!form.isValid()) {
            let bad = form.getFields().findBy((f) => !f.isValid());
            if (bad) {
                let tabs = me.down('tabpanel');
                let tab = tabs.items.findBy((t) => t.contains(bad));
                if (tab) {
                    tabs.setActiveTab(tab);
                }
                bad.focus();
            }
            return;
        }
        me.setLoading(gettext('Saving...'));
        PVE.UnifiDNS.call('/api/config', me.values()).then(
            () => {
                me.setLoading(false);
                me.onSaved();
                me.close();
            },
            (err) => {
                me.setLoading(false);
                PVE.UnifiDNS.error(err);
            },
        );
    },

    setNodeDns: function () {
        let me = this;
        let host = (me.field('UNIFI_HOST').getValue() || '').trim();
        let domain = (me.field('DOMAIN').getValue() || '').trim().toLowerCase();
        if (!host || !domain) {
            PVE.UnifiDNS.error(gettext('Fill in Console Address and Domain first.'));
            return;
        }
        Ext.Msg.confirm(gettext('Node DNS'),
            Ext.String.format(gettext('Set {0} to use DNS server {1} with search domain {2}?'),
                Ext.String.htmlEncode(Proxmox.NodeName), Ext.String.htmlEncode(host), Ext.String.htmlEncode(domain)),
            (btn) => {
                if (btn !== 'yes') {
                    return;
                }
                PVE.UnifiDNS.pve('/nodes/' + Proxmox.NodeName + '/dns', 'PUT', { dns1: host, search: domain })
                    .then(() => me.reloadNode(), PVE.UnifiDNS.error);
            });
    },

    installLldp: function () {
        let me = this;
        me.setLoading(gettext('Installing lldpd...'));
        PVE.UnifiDNS.call('/api/node/lldpd', {}).then(
            () => {
                me.setLoading(false);
                me.reloadNode();
            },
            (err) => {
                me.setLoading(false);
                PVE.UnifiDNS.error(err);
            },
        );
    },

    toggleTimer: function () {
        let me = this;
        let on = !(me.nodeState && me.nodeState.timer.active);
        PVE.UnifiDNS.call('/api/node/timer', { enabled: on }).then(() => me.reloadNode(), PVE.UnifiDNS.error);
    },

    initComponent: function () {
        let me = this;
        let tabDefaults = { labelWidth: 160, anchor: '100%' };
        let note = (html) => ({ xtype: 'component', html: html, padding: '0 0 10 0', cls: 'pmx-hint' });
        let check = (name, label, box) => ({
            xtype: 'checkbox', name: name, fieldLabel: label, boxLabel: box,
            inputValue: 'true', uncheckedValue: 'false',
        });
        let action = (label, itemId, btnId, text, handler) => ({
            xtype: 'fieldcontainer',
            fieldLabel: label,
            layout: 'hbox',
            items: [
                { xtype: 'displayfield', itemId: itemId, flex: 1, value: '' },
                { xtype: 'button', itemId: btnId, text: text, margin: '0 0 0 8', handler: handler },
            ],
        });

        me.form = Ext.create('Ext.form.Panel', {
            border: false,
            layout: 'fit',
            items: [{
                xtype: 'tabpanel',
                deferredRender: false,
                border: false,
                items: [
                    {
                        title: 'UniFi',
                        xtype: 'container',
                        layout: 'anchor',
                        padding: 10,
                        scrollable: 'y',
                        defaults: tabDefaults,
                        items: [
                            note(gettext('Create an API key in UniFi OS under Settings, Control Plane, Integrations. It is stored on this node in /etc/pve/priv, readable by root only. The console certificate is pinned when you save; changing the console address needs the key again.')),
                            {
                                xtype: 'textfield', name: 'UNIFI_HOST', fieldLabel: gettext('Console Address'),
                                allowBlank: false, emptyText: '192.168.1.1',
                                listeners: { change: () => me.field('UNIFI_CERT_SHA256').setValue('') },
                            },
                            { xtype: 'hiddenfield', name: 'UNIFI_CERT_SHA256' },
                            { xtype: 'textfield', name: 'UNIFI_API_KEY', fieldLabel: gettext('API Key'), inputType: 'password', allowBlank: false },
                            { xtype: 'textfield', name: 'UNIFI_SITE', fieldLabel: gettext('Site'), allowBlank: false, value: 'default' },
                            check('VERIFY_TLS', gettext('Verify Certificate'), gettext('only if the console has a trusted certificate')),
                            {
                                xtype: 'textfield', name: 'DOMAIN', fieldLabel: gettext('Domain'), vtype: 'DnsName',
                                allowBlank: false, emptyText: 'lan.example.com',
                                listeners: { change: () => me.updateCertHelp() },
                            },
                            note(gettext('Records are named <guest>.<domain> and <node>.<domain>. Use the Domain Name set on your UniFi networks. For a trusted certificate use a subdomain of a domain you own, for example lan.example.com. Changing it moves every record on the next sync.')),
                            { xtype: 'displayfield', itemId: 'testResult', fieldLabel: gettext('Connection'), value: gettext('not tested yet') },
                        ],
                    },
                    {
                        title: gettext('Options'),
                        xtype: 'container',
                        layout: 'anchor',
                        padding: 10,
                        scrollable: 'y',
                        defaults: tabDefaults,
                        items: [
                            check('PUBLISH_NODES', gettext('Node Names'), gettext('publish <node>.<domain> for this node')),
                            check('INCLUDE_STOPPED', gettext('Stopped Guests'), gettext('keep records while a guest is stopped')),
                            check('WRITE_NOTES', gettext('Guest Notes'), gettext('add a "UniFi DNS:" status line to each guest\'s Notes')),
                            { xtype: 'textfield', name: 'ONLY_TAG', fieldLabel: gettext('Only Guests Tagged'), emptyText: gettext('all guests'), regex: /^[a-z0-9_.+-]*$/ },
                            check('SYNC_CLIENT_NAMES', gettext('UniFi Client Names'), gettext('name each guest in UniFi\'s client list after the guest')),
                            check('SYNC_CLIENT_ICONS', gettext('UniFi Client Icons'), gettext('set icons: chosen, matched by name, or the default below')),
                            { xtype: 'textfield', name: 'DEFAULT_ICON', fieldLabel: gettext('Default Icon ID'), regex: /^\d{0,7}$/, emptyText: '5254 = Proxmox' },
                            { xtype: 'textfield', name: 'RUN_ON_NODE', fieldLabel: gettext('Node That Syncs'), hidden: true, regex: /^[A-Za-z0-9-]*$/ },
                            note(gettext('Guest tags work as switches: nodns hides a guest, dns-NAME renames its record, dns-alias-NAME adds another name.')),
                        ],
                    },
                    {
                        title: gettext('This Node'),
                        xtype: 'container',
                        layout: 'anchor',
                        padding: 10,
                        scrollable: 'y',
                        defaults: tabDefaults,
                        items: [
                            { xtype: 'displayfield', fieldLabel: gettext('Node'), value: Proxmox.NodeName },
                            action(gettext('Node DNS'), 'nodeDns', 'dnsBtn', gettext('Use UniFi'), () => me.setNodeDns()),
                            action('lldpd', 'lldp', 'lldpBtn', gettext('Install'), () => me.installLldp()),
                            action(gettext('Automatic Sync'), 'timer', 'timerBtn', gettext('Turn On'), () => me.toggleTimer()),
                            note(gettext('Use UniFi points this node\'s own DNS at the UniFi console and sets its search domain. lldpd lets UniFi draw this node\'s guests under it in the topology.')),
                        ],
                    },
                    {
                        title: gettext('Certificate'),
                        xtype: 'container',
                        padding: 10,
                        scrollable: 'y',
                        items: [
                            { xtype: 'component', itemId: 'certHelp', html: '', padding: '0 0 10 0' },
                            { xtype: 'pveACMEClusterView', border: true, height: 330 },
                            { xtype: 'pveCertificatesView', nodename: Proxmox.NodeName, border: true, height: 470, margin: '10 0 0 0' },
                        ],
                    },
                ],
            }],
        });

        me.items = [me.form];
        me.buttons = [
            { text: gettext('Test Connection'), handler: () => me.test() },
            '->',
            { text: gettext('Save'), handler: () => me.save() },
            { text: gettext('Cancel'), handler: () => me.close() },
        ];

        me.callParent();
        me.on('afterrender', () => me.load(), me, { single: true });
    },
});

// ---------------------------------------------------------------- panel ----

Ext.define('PVE.UnifiDNS.Panel', {
    extend: 'Ext.panel.Panel',
    alias: 'widget.pveUnifiDnsPanel',

    layout: { type: 'vbox', align: 'stretch' },
    border: false,

    short: function (fqdn) {
        let me = this;
        if (!fqdn || !me.state) {
            return fqdn || '';
        }
        let suffix = '.' + me.state.domain;
        return fqdn.endsWith(suffix) ? fqdn.slice(0, -suffix.length) : fqdn;
    },

    keepSelection: function (grid, store, rows) {
        let sel = grid.getSelection()[0];
        let id = sel ? sel.getId() : undefined;
        store.loadData(rows);
        if (id !== undefined) {
            let rec = store.getById(id);
            if (rec) {
                grid.getSelectionModel().select(rec);
            }
        }
    },

    mask: function (msg) {
        let me = this;
        Proxmox.Utils.setErrorMask(me.guestGrid, msg);
        Proxmox.Utils.setErrorMask(me.recordGrid, msg ? (msg === true ? true : ' ') : false);
    },

    fill: function (s) {
        let me = this;
        let rows = [];
        (s.nodes || []).forEach((n) =>
            rows.push({
                key: 'node:' + n.name, kind: 'node', vmid: '', name: n.name,
                unifi_name: n.name + ' (' + gettext('Node') + ')', node: n.node, status: n.status,
                record: me.short(n.record), ip: n.ip, source: n.source, notes: n.note || '',
                sortkey: '0',
            }));
        (s.guests || []).forEach((g) => {
            let c = g.client || {};
            let notes = [];
            if (g.aliases && g.aliases.length) {
                notes.push(gettext('Aliases') + ': ' + g.aliases.map((a) => me.short(a)).join(', '));
            }
            if (g.note) {
                notes.push(g.note);
            }
            if (!c.in_unifi) {
                notes.push(gettext('not in the UniFi client list yet'));
            }
            rows.push({
                key: 'guest:' + g.vmid, kind: 'guest', vmid: g.vmid, name: g.name,
                unifi_name: c.unifi_name || g.name, node: g.node, status: g.status,
                record: me.short(g.record), ip: g.ip, source: g.source, notes: notes.join(', '),
                icon_id: c.icon_id, in_unifi: !!c.in_unifi,
                icon_title: c.icon_name ? c.icon_name + ' (' + c.icon_source + ')' : '',
                sortkey: '1' + String(g.vmid).padStart(9, '0'),
            });
        });
        me.keepSelection(me.guestGrid, me.guestStore, rows);
        me.keepSelection(me.recordGrid, me.recordStore, s.records || []);

        let published = (s.guests || []).filter((g) => g.record).length;
        me.statusText.setText(Ext.String.format(
            gettext('Domain: {0}, {1} published, last sync {2}'),
            Ext.String.htmlEncode(s.domain), published, PVE.UnifiDNS.ago(s.time),
        ) + (s.sync_running ? ', ' + gettext('syncing') + '...' : ''));
        let owned = (s.records || []).filter((r) => r.owned).length;
        me.recordText.setText(Ext.String.format(gettext('{0} from the sync, {1} manual'),
            owned, (s.records || []).length - owned));
        me.down('#syncnow').setDisabled(!!s.sync_running);
    },

    reload: function (quiet) {
        let me = this;
        clearTimeout(me.timer);
        PVE.UnifiDNS.call('/api/state').then(
            (s) => {
                if (me.destroyed) {
                    return;
                }
                me.state = s;
                if (!s.configured) {
                    me.statusText.setText(gettext('Not set up yet'));
                    me.down('#syncnow').setDisabled(true);
                    me.mask(gettext('Not set up yet. Click Setup to connect Proxmox to UniFi.'));
                    if (!me.setupShown) {
                        me.setupShown = true;
                        me.openSetup();
                    }
                    return;
                }
                me.mask(false);
                me.fill(s);
                me.timer = setTimeout(() => me.reload(true), s.sync_running ? 4000 : 30000);
            },
            (err) => {
                if (me.destroyed) {
                    return;
                }
                if (!me.state || !quiet) {
                    me.mask(Ext.String.htmlEncode(err.message));
                }
                me.timer = setTimeout(() => me.reload(true), 30000);
            },
        );
    },

    act: function (path, body) {
        let me = this;
        return PVE.UnifiDNS.call(path, body).then(
            () => {
                setTimeout(() => {
                    if (!me.destroyed) {
                        me.reload(true);
                    }
                }, 1500);
                return true;
            },
            (err) => {
                PVE.UnifiDNS.error(err);
                return false;
            },
        );
    },

    openSetup: function () {
        let me = this;
        Ext.create('PVE.UnifiDNS.SetupWindow', {
            onSaved: () => {
                me.mask(true);
                setTimeout(() => !me.destroyed && me.reload(), 1500);
            },
        }).show();
    },

    openAddRecord: function () {
        let me = this;
        let win = Ext.create('Ext.window.Window', {
            title: gettext('Add') + ': ' + gettext('DNS Record'),
            modal: true,
            width: 420,
            resizable: false,
            border: false,
            layout: 'fit',
            items: [{
                xtype: 'form',
                bodyPadding: 10,
                border: false,
                fieldDefaults: { labelWidth: 100, anchor: '100%' },
                items: [
                    {
                        xtype: 'proxmoxKVComboBox',
                        name: 'action',
                        fieldLabel: gettext('Type'),
                        value: 'add',
                        allowBlank: false,
                        editable: false,
                        comboItems: [['add', 'A'], ['add-cname', 'CNAME']],
                        listeners: {
                            change: function (field, value) {
                                let vf = win.down('field[name=value]');
                                let isA = value === 'add';
                                vf.setFieldLabel(isA ? gettext('IP Address') : gettext('Target'));
                                vf.vtype = isA ? 'IPAddress' : 'DnsName';
                                vf.validate();
                            },
                        },
                    },
                    {
                        xtype: 'textfield',
                        name: 'name',
                        fieldLabel: gettext('Name'),
                        emptyText: 'printer',
                        vtype: 'DnsName',
                        allowBlank: false,
                    },
                    {
                        xtype: 'textfield',
                        name: 'value',
                        fieldLabel: gettext('IP Address'),
                        vtype: 'IPAddress',
                        allowBlank: false,
                    },
                ],
                buttons: [{
                    text: gettext('Add'),
                    formBind: true,
                    handler: function () {
                        let values = win.down('form').getValues();
                        me.act('/api/record', values).then((ok) => {
                            if (ok) {
                                win.close();
                            }
                        });
                    },
                }],
            }],
        });
        win.show();
    },

    openIconPicker: function (rec) {
        let me = this;
        let store = Ext.create('Ext.data.Store', { fields: ['id', 'name'] });
        let sm = Ext.create('Ext.selection.RowModel', {});
        let win;
        let choose = function (id) {
            win.close();
            me.act('/api/icon', { vmid: rec.data.vmid, icon: id });
        };
        win = Ext.create('Ext.window.Window', {
            title: Ext.String.format(gettext('Icon for {0}'), Ext.String.htmlEncode(rec.data.name)),
            modal: true,
            width: 480,
            height: 460,
            border: false,
            layout: 'fit',
            items: [{
                xtype: 'grid',
                store: store,
                selModel: sm,
                border: false,
                hideHeaders: true,
                deferEmptyText: false,
                emptyText: gettext("Type at least two letters to search UniFi's device catalogue"),
                tbar: [{
                    xtype: 'textfield',
                    emptyText: gettext('Search'),
                    flex: 1,
                    listeners: {
                        change: {
                            buffer: 300,
                            fn: function (field, value) {
                                let q = (value || '').trim();
                                if (q.length < 2) {
                                    store.removeAll();
                                    return;
                                }
                                PVE.UnifiDNS.call('/api/icons?q=' + encodeURIComponent(q)).then(
                                    (list) => {
                                        if (!win.destroyed) {
                                            store.loadData(list);
                                        }
                                    },
                                    (err) => PVE.UnifiDNS.error(err),
                                );
                            },
                        },
                    },
                }],
                columns: [
                    { dataIndex: 'id', width: 56, renderer: (v) => PVE.UnifiDNS.iconHtml(v, 32) },
                    { dataIndex: 'name', flex: 1, renderer: Ext.String.htmlEncode },
                ],
                listeners: {
                    itemdblclick: (view, r) => choose(r.data.id),
                },
            }],
            buttons: [
                {
                    text: gettext('Automatic'),
                    tooltip: gettext('Matched by name, or the default icon'),
                    handler: () => choose('auto'),
                },
                '->',
                {
                    xtype: 'proxmoxButton',
                    text: gettext('OK'),
                    selModel: sm,
                    disabled: true,
                    handler: (btn, event, r) => choose(r.data.id),
                },
                { text: gettext('Cancel'), handler: () => win.close() },
            ],
        });
        win.show();
        win.down('textfield').focus(false, 200);
    },

    initComponent: function () {
        let me = this;

        me.guestStore = Ext.create('Ext.data.Store', {
            model: 'pve-unifidns-guest',
            sorters: 'sortkey',
        });
        me.recordStore = Ext.create('Ext.data.Store', {
            model: 'pve-unifidns-record',
            sorters: [
                { property: 'owned', direction: 'DESC' },
                { property: 'name', direction: 'ASC' },
            ],
        });
        me.statusText = Ext.create('Ext.toolbar.TextItem', { text: '' });
        me.recordText = Ext.create('Ext.toolbar.TextItem', { text: '' });

        let guestSm = Ext.create('Ext.selection.RowModel', {});
        let recordSm = Ext.create('Ext.selection.RowModel', {});
        let canIcon = (rec) => rec.data.kind === 'guest' && rec.data.in_unifi && me.state && me.state.icons;

        me.tbar = [
            {
                text: gettext('Setup'),
                iconCls: 'fa fa-cog',
                handler: () => me.openSetup(),
            },
            '-',
            {
                text: gettext('Sync Now'),
                itemId: 'syncnow',
                handler: () => me.act('/api/sync', {}),
            },
            {
                text: gettext('Reload'),
                handler: () => me.reload(),
            },
            '->',
            me.statusText,
        ];

        me.guestGrid = Ext.create('Ext.grid.Panel', {
            title: gettext('Guests'),
            flex: 3,
            store: me.guestStore,
            selModel: guestSm,
            tbar: [
                {
                    xtype: 'proxmoxButton',
                    text: gettext('Change Icon'),
                    selModel: guestSm,
                    disabled: true,
                    enableFn: canIcon,
                    handler: (btn, event, rec) => me.openIconPicker(rec),
                },
            ],
            columns: [
                {
                    dataIndex: 'icon_id',
                    width: 44,
                    sortable: false,
                    menuDisabled: true,
                    renderer: function (v, meta, rec) {
                        if (rec.data.icon_title) {
                            meta.tdAttr = 'data-qtip="' +
                                Ext.htmlEncode(Ext.htmlEncode(rec.data.icon_title)) + '"';
                        }
                        return PVE.UnifiDNS.iconHtml(v);
                    },
                },
                { header: gettext('Name in UniFi'), dataIndex: 'unifi_name', flex: 2, minWidth: 140, renderer: Ext.String.htmlEncode },
                { header: 'ID', dataIndex: 'vmid', width: 70 },
                { header: gettext('Node'), dataIndex: 'node', width: 80, renderer: Ext.String.htmlEncode },
                { header: gettext('Status'), dataIndex: 'status', width: 80 },
                { header: gettext('DNS Name'), dataIndex: 'record', flex: 2, minWidth: 120, renderer: Ext.String.htmlEncode },
                { header: gettext('IP Address'), dataIndex: 'ip', width: 120 },
                { header: gettext('Source'), dataIndex: 'source', width: 70 },
                { header: gettext('Notes'), dataIndex: 'notes', flex: 3, minWidth: 140, renderer: Ext.String.htmlEncode },
            ],
            bbar: [{
                xtype: 'tbtext',
                text: gettext('Rename a guest in Proxmox to rename it in UniFi. Guest tags: nodns hides it, dns-NAME renames its record, dns-alias-NAME adds another name.'),
            }],
            listeners: {
                itemdblclick: (view, rec) => {
                    if (canIcon(rec)) {
                        me.openIconPicker(rec);
                    }
                },
            },
        });

        me.recordGrid = Ext.create('Ext.grid.Panel', {
            title: gettext('DNS Records in UniFi'),
            flex: 2,
            store: me.recordStore,
            selModel: recordSm,
            tbar: [
                {
                    text: gettext('Add'),
                    handler: () => me.openAddRecord(),
                },
                {
                    xtype: 'proxmoxButton',
                    text: gettext('Remove'),
                    selModel: recordSm,
                    disabled: true,
                    dangerous: true,
                    enableFn: (rec) => !rec.data.owned,
                    confirmMsg: (rec) => Ext.String.format(
                        gettext('Are you sure you want to remove entry {0}'),
                        "'" + Ext.String.htmlEncode(rec.data.name) + "'",
                    ),
                    handler: (btn, event, rec) => me.act('/api/record', { action: 'remove', name: rec.data.name }),
                },
                '->',
                me.recordText,
            ],
            columns: [
                { header: gettext('Type'), dataIndex: 'type', width: 80 },
                {
                    header: gettext('Name'),
                    dataIndex: 'name',
                    flex: 1,
                    renderer: (v, meta, rec) => Ext.String.htmlEncode(v) +
                        (rec.data.enabled ? '' : ' (' + gettext('disabled') + ')'),
                },
                { header: gettext('Target'), dataIndex: 'value', flex: 1, renderer: Ext.String.htmlEncode },
                {
                    header: gettext('Managed by'),
                    dataIndex: 'owned',
                    width: 120,
                    renderer: (v) => (v ? gettext('Sync') : gettext('Manual')),
                },
            ],
        });

        me.items = [me.guestGrid, { xtype: 'splitter' }, me.recordGrid];

        me.callParent();

        me.on('afterrender', () => {
            me.mask(true);
            me.reload();
        });
        me.on('destroy', () => clearTimeout(me.timer));
    },
});

// Only hook in when this Proxmox version has the classes we extend; otherwise do nothing.
if (window.PVE && PVE.panel && PVE.panel.Config && PVE.dc && PVE.dc.Config) {
    Ext.define('PVE.UnifiDNS.DcTab', {
        override: 'PVE.panel.Config',

        initComponent: function () {
            let me = this;
            if (Ext.getClassName(me) === 'PVE.dc.Config' && Ext.isArray(me.items)) {
                let caps = Ext.state.Manager.get('GuiCap') || {};
                if (caps.dc && caps.dc['Sys.Audit']) {
                    let at = -1;
                    Ext.Array.each(me.items, (it, i) => {
                        if (it && it.itemId === 'notes') {
                            at = i;
                        }
                    });
                    Ext.Array.insert(me.items, at >= 0 ? at + 1 : me.items.length, [{
                        xtype: 'pveUnifiDnsPanel',
                        itemId: 'unifidns',
                        title: 'UniFi DNS',
                        iconCls: 'fa fa-globe',
                    }]);
                }
            }
            me.callParent(arguments);
        },
    });
}
