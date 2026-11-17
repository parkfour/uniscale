# Multi-User Setup

UniScale can keep a separate, isolated vault per person: one server, one dashboard URL,
each person sees only their own measurements, labs, DNA and supplements.

UniScale does **not** implement logins itself. Authentication is delegated to what you
already run in front of it: a reverse proxy doing HTTP Basic auth, OAuth / OIDC, or mutual TLS
(client certificates). The proxy tells UniScale *who* the request is for in one header.
Devices that can't go through an interactive login (the Android app, scripts) can use
per-device tokens instead, or client certificates.

---

## 1. Enable multi-user mode

In `~/.config/uniscale/config.toml`:

```toml
[server]
host = "127.0.0.1"          # keep UniScale on loopback; only the proxy should reach it

[auth]
mode = "multi"
user_header = "X-Remote-User"            # the header your proxy sets
trusted_proxies = ["127.0.0.1", "::1"]   # who may set it (addresses or CIDRs)

# Optional: per-device tokens -> user. Several tokens may map to the same user.
[auth.tokens]
"<long random token for alice's phone>"  = "alice"
"<long random token for alice's laptop>" = "alice"
"<long random token for bob's phone>"    = "bob"
```

Generate tokens with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.

### How a request is assigned to a user

1. If it carries a token from `[auth.tokens]` (as `Authorization: Bearer <token>`, or as the
   password of HTTP Basic auth), it belongs to that token's user. This works with or without a proxy.
2. Otherwise, if the request comes from an address in `trusted_proxies`, the user is read from
   `user_header`.
3. Otherwise it is rejected with `401`. **There is no default or fallback user**, so a
   misconfigured proxy fails loudly instead of mixing people's data.

`user_header` from any address *not* in `trusted_proxies` is ignored, so clients that reach
UniScale directly can't claim to be someone else. `server.api_token` (single-user mode) is not
accepted in multi-user mode; give each device a token in `[auth.tokens]` instead.

User names are lower-cased and must match `[a-z0-9][a-z0-9._@-]*` (max 128 chars), so plain
names (`alice`) and e-mail addresses (`alice@example.com`) both work. Anything else (spaces,
slashes, leading dots) is rejected with `403`. Make your certificate CNs or OAuth claims match.

### Where data lives

```text
<data_dir>/                      # [general] data_dir, default ~/.config/uniscale
├── config.toml
└── users/
    ├── alice/
    │   ├── measurements.jsonl   # weight, BP, ECG, labs, DNA, sleep, supplements...
    │   ├── profile.json         # name, date of birth, sex, ASCVD risk factors (👤 Profile)
    │   ├── supplements_stack.json
    │   └── ecg/                 # daemon ECG recordings
    └── bob/
        └── ...
```

A user's folder is created on their first write. Back up or export one person by copying their folder.

### Moving existing single-user data to a user

```bash
cd ~/.config/uniscale
mkdir -p users/alice
mv measurements.jsonl users/alice/
[ -f supplements_stack.json ] && mv supplements_stack.json users/alice/
[ -f profile.json ] && mv profile.json users/alice/
[ -d ecg ] && mv ecg users/alice/
```

---

## 2. Reverse proxy recipes

Each recipe **overwrites** `X-Remote-User` with the authenticated identity, so a value sent by
the client never reaches UniScale. Replace `health.example.com` and paths with your own.

### HTTP Basic auth

**Apache**
```apache
<Location />
    AuthType Basic
    AuthName "UniScale"
    AuthUserFile /etc/apache2/uniscale.htpasswd
    Require valid-user
    RequestHeader set X-Remote-User "expr=%{REMOTE_USER}"
    RequestHeader unset Authorization       # don't forward the proxy's Basic credentials
</Location>
ProxyPass        / http://127.0.0.1:8088/
ProxyPassReverse / http://127.0.0.1:8088/
```

**nginx**
```nginx
location / {
    auth_basic           "UniScale";
    auth_basic_user_file /etc/nginx/uniscale.htpasswd;
    proxy_set_header     X-Remote-User $remote_user;
    proxy_set_header     Authorization "";    # don't forward the proxy's Basic credentials
    proxy_pass           http://127.0.0.1:8088;
}
```

### OAuth / OIDC (Google, GitHub, Keycloak, Authelia, ...)

With [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) (run with `--set-xauthrequest`)
behind nginx `auth_request`:
```nginx
location /oauth2/ {
    proxy_pass http://127.0.0.1:4180;
}
location / {
    auth_request /oauth2/auth;
    error_page 401 = /oauth2/sign_in;
    auth_request_set $user $upstream_http_x_auth_request_email;   # or ..._user
    proxy_set_header X-Remote-User $user;
    proxy_pass http://127.0.0.1:8088;
}
```
Authelia, Authentik and Pomerium work the same way: set `user_header` to the header they emit
(e.g. `Remote-User`), or copy it into `X-Remote-User` as above.

### Mutual TLS (client certificates)

The user is the certificate's subject **Common Name (CN)**. Only accept certificates issued by your own CA.

**Apache**
```apache
SSLVerifyClient require
SSLVerifyDepth  1
SSLCACertificateFile /etc/ssl/uniscale/ca.crt
# SSLCARevocationFile /etc/ssl/uniscale/ca.crl     # to revoke individual devices
RequestHeader set X-Remote-User "%{SSL_CLIENT_S_DN_CN}s"
ProxyPass        / http://127.0.0.1:8088/
ProxyPassReverse / http://127.0.0.1:8088/
```

**nginx** (nginx exposes the whole subject DN, so extract the CN):
```nginx
map $ssl_client_s_dn $ssl_client_cn {
    default "";
    ~(^|,)CN=(?<cn>[^,]+) $cn;
}
server {
    listen 443 ssl;
    ssl_client_certificate /etc/ssl/uniscale/ca.crt;
    ssl_verify_client on;
    # ssl_crl /etc/ssl/uniscale/ca.crl;
    location / {
        proxy_set_header X-Remote-User $ssl_client_cn;
        proxy_pass http://127.0.0.1:8088;
    }
}
```

---

## 3. One person, several devices

All of a person's devices end up in the same vault as long as they present the same identity.

### Client certificates: one per device, same CN (recommended)

Issue a separate certificate for each device, all with `CN=alice`. Put the device in another
field (`OU`) so logs and revocation can tell them apart:

```bash
# per device: key + CSR with the shared CN and a device-specific OU
openssl req -new -newkey rsa:3072 -nodes -keyout alice-phone.key -out alice-phone.csr \
    -subj "/CN=alice/OU=phone"
openssl x509 -req -in alice-phone.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -days 825 -out alice-phone.crt
openssl pkcs12 -export -inkey alice-phone.key -in alice-phone.crt -out alice-phone.p12   # for Android
```

If a device is lost, revoke just its certificate (by serial) in your CRL; Alice's other devices
keep working.

**Sharing one certificate across devices also works**, since UniScale only sees the CN. The
trade-offs: revoking it locks out every device at once (re-issue and reinstall everywhere),
the private key has to be exportable and copied around (a leak from any device compromises all),
and logs can't tell the devices apart.

### Tokens: one per device, same user

Add one `[auth.tokens]` line per device, all mapping to the same user. To retire a device,
delete its line and restart the server.

---

## 4. CLI and daemon

Commands that read or write data need to know whose vault to use:

```bash
uniscale --user alice import-lab ~/Downloads/quest.pdf
UNISCALE_USER=bob uniscale history
```

Without `--user` / `UNISCALE_USER`, multi-user mode refuses (exit code 2) rather than guessing.
The BLE daemon records into the vault of the user it was started for
(`uniscale --user alice daemon`); routing shared scales and cuffs to the right person
automatically is a separate, future feature.

---

## 5. Security checklist

- [ ] `server.host` is `127.0.0.1` (or a private address only the proxy can reach).
- [ ] `trusted_proxies` lists only your proxy's address(es).
- [ ] Every proxy route to UniScale authenticates *and* sets `user_header`; none forwards it from the client.
- [ ] mTLS: the proxy trusts only your own CA, and you control who can issue certificates
      (anyone holding a certificate with `CN=bob` *is* bob).
- [ ] Tokens are long and random, one per device, and removed when a device is retired.

## 6. Profiles

Each person sets their own name, date of birth, sex and ASCVD risk factors (smoker, diabetic, on
blood-pressure medication) from the dashboard's **👤 Profile** button, `PUT /api/profile`, or
`uniscale --user alice profile --name Alice --date-of-birth 1980-06-15 --sex female` (`--birth-year 1980`
instead of the full date also works). Their name replaces the user id in the dashboard header. They are stored in that person's
`profile.json`. In multi-user mode the `[profile]` section of `config.toml` is ignored, so one
person's details are never applied to another; 10-year ASCVD risk is only computed once sex is set.

## 7. Not yet per-user

- `[[supplements.stack]]` in `config.toml` is the starting stack for everyone until they edit
  their own (each person's edited stack is per-user).
- Garmin Connect cloud sync (`[legacy_cloud.garmin]`) uses one account for the whole server.
