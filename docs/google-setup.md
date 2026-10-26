# Google sign in setup

How to create the Google Cloud OAuth client that "Continue with Google" uses. The backend needs only the client ID (`GOOGLE_CLIENT_ID`); there is no client secret and no redirect URI in this flow, because the browser receives an ID token from Google Identity Services and the backend verifies it against Google's published keys.

Google changes its console screens from time to time. The steps below follow the Google Auth Platform pages of the Google Cloud console at the time of writing; if a label differs, look for the equivalent item.

## 1. Create a project

1. Open the [Google Cloud console](https://console.cloud.google.com/) and sign in with the Google account that will own the project.
2. In the project picker at the top, choose **New project**. Name it, for example, `AgriSence`, and create it. Make sure the new project is selected afterwards.

## 2. Google Auth Platform: branding

1. Open **Google Auth Platform** (search for it in the console search bar). If asked, choose **Get started**.
2. **Branding**: set the app name (`AgriSence`), a user support email and the developer contact email. A logo is optional; adding one can require Google's verification later, so leave it out while testing.
3. Save.

## 3. Audience: External, in Testing

1. **Audience**: choose **External** (anyone with a Google account, not only one Google Workspace organisation).
2. Leave the publishing status on **Testing**.
3. Under **Test users**, add the Gmail addresses of every person who will try Google sign in (yourself, your supervisor, testers). While the app is in Testing, only these accounts can sign in; everyone else sees "Access blocked".

## 4. Data access: openid, email, profile only

1. **Data access**: add only the scopes `openid`, `.../auth/userinfo.email` (`email`) and `.../auth/userinfo.profile` (`profile`). These are non-sensitive scopes.
2. Do not add any other scope. The backend reads only the subject, the verified email and the name from the ID token.

## 5. Create the OAuth client (Web application)

1. **Clients** → **Create client**.
2. **Application type**: **Web application**. Name it, for example, `AgriSence web`.
3. **Authorised JavaScript origins**: add
   - `http://localhost:5173` (the Vite development server)
   - `http://localhost`
   - later, the deployed frontend origin, for example `https://app.example.com` (exact scheme and host, no path, no trailing slash).
4. **Authorised redirect URIs**: leave empty. This flow does not redirect.
5. Create. Copy the **Client ID** (it ends with `.apps.googleusercontent.com`). A client secret may be shown; this flow does not use it, so do not copy it anywhere.

## 6. Configure the backend and the frontend

- Backend `.env`: `GOOGLE_CLIENT_ID=<the client ID>`. Run `python scripts/check_config.py`: it reports `GOOGLE_CLIENT_ID` as OK, MISSING (Google sign in disabled, not an error) or INVALID (wrong format), and never prints the value. Restart the API.
- Frontend `.env`: `VITE_GOOGLE_CLIENT_ID=<the same client ID>`.
- Apply the migration `supabase/migrations/20261027000100_google_sign_in.sql` and run `supabase/verification/google_check.sql`.

The client ID is not a secret (it is visible in the browser), but keep it in configuration rather than in code so development and production can use different clients.

## 7. Propagation delays

New clients and changed origins can take from a few minutes up to a few hours to take effect. If sign in fails right after a change with `origin_mismatch` or a similar error, wait and retry before changing anything else.

## Common errors

| Symptom | Cause and fix |
| --- | --- |
| `Error 400: origin_mismatch` or "The given origin is not allowed" | The page's origin is not in **Authorised JavaScript origins**. Add the exact origin (scheme, host and port, for example `http://localhost:5173`) and wait for propagation. |
| "Access blocked: AgriSence has not completed the Google verification process" or "This app is blocked" | The app is in Testing and the Google account is not a test user. Add it under **Audience → Test users**. |
| The API answers 503 `google_unavailable` | `GOOGLE_CLIENT_ID` is empty in the backend, or Google's key endpoint cannot be reached from the server. |
| The API answers 401 `google_sign_in_failed` for every attempt | The frontend and backend use different client IDs (the token's audience does not match), the server clock is far off, or the frontend reuses a nonce. Request a new nonce for every attempt. |
| The Google popup closes immediately or never reports back | The frontend host sends `Cross-Origin-Opener-Policy: same-origin`. Use `same-origin-allow-popups` or no such header. |

## Publishing

While the app is in Testing, only listed test users can sign in. Publishing the app (moving it to **In production**) makes Google sign in available to every Google account. Whether verification is needed, and what it requires, depends on the scopes, the branding (for example a logo) and Google's current policy: check Google's own pages on OAuth app verification and the publishing status before publishing. The `openid`, `email` and `profile` scopes used here are non-sensitive.
