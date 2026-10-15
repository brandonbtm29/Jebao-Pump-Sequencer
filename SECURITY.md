# Security

## Reporting a problem

Please report security problems privately through GitHub: open the **Security** tab of this repository and choose **Report a vulnerability**. Please don't open a public issue for them.

## Security model

- The app needs your Jebao account email and password, which it stores in plain text in `secrets.json`. That file lives in the app folder or in the settings folder you choose. It is gitignored, but anyone who can read that folder can read your login. If you sync the settings folder, keep it private.
- The app only talks to the Jebao (Gizwits) cloud, over HTTPS. It sends your login there to get a session token, and it sends pump commands.
- The Gizwits application ID in `jebao_gui.py` identifies the Jebao app to the cloud. It is the same value the official app uses. It is not a secret and doesn't give access to anyone's pumps.
- Never commit `secrets.json`, `jebao_config.json` or packet captures.
