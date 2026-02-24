# M&N Driver Portal (Demo)

Login: `driver`
Password: `driver`

Logo sourced from your site: https://mnauto.us/wp-content/uploads/2025/12/LOGO-White-scaled.png


## Push + Chat
Set env vars:
- VAPID_PUBLIC_KEY
- VAPID_PRIVATE_KEY
- VAPID_SUBJECT (e.g. mailto:dispatch@mnauto.us)
- TAWK_SRC (e.g. https://embed.tawk.to/XXXX/default)


## Reminders cron
Set CRON_TOKEN and call POST /cron/daily?token=YOURTOKEN daily.
