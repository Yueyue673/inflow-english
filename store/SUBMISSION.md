# Chrome Web Store submission checklist

This is the remaining one-time account-owner path. Code/build tasks are automated; fee, agreements, identity/contact verification and final submission cannot be delegated.

## Before opening the dashboard

- [x] Manifest V3 archive built from an exact allowlist.
- [x] Required permission reduced to `storage`.
- [x] Required host reduced to `https://www.youtube.com/*`.
- [x] Loopback access moved to optional permission requested from a user gesture.
- [x] Remote code absent.
- [x] Privacy policy published: <https://github.com/Yueyue673/inflow-english/blob/main/PRIVACY.md>
- [x] Support issues published: <https://github.com/Yueyue673/inflow-english/issues>
- [x] Source published: <https://github.com/Yueyue673/inflow-english>
- [x] 128×128 icon prepared: `assets/icon-128.png`.
- [x] Two 1280×800 product screenshots prepared.
- [x] Listing copy and permission justifications prepared in [`LISTING.md`](LISTING.md).

## Account-owner steps

1. Register or open the Chrome Web Store developer dashboard.
2. Accept the developer agreement and pay Google's one-time registration fee if the account has not done so.
3. Verify the contact email.
4. Create a new item and upload `InFlow-English-Chrome-0.2.6-CWS-first-upload.zip` from the GitHub Release. Do not upload the development ZIP: its fixed development `key` is intentionally absent from the first-store-upload package, following Chrome's [manifest key workflow](https://developer.chrome.com/docs/extensions/reference/manifest/key).
5. Record the Store item ID assigned after upload. If the optional local service will be tested, add `chrome-extension://<STORE_ID>` to `INFLOW_ALLOWED_EXTENSION_ORIGINS`; never replace it with a guessed ID.
6. Copy the name, short description, detailed description, category and language from `LISTING.md`.
7. Upload the icon and the two screenshots from `store/assets/`.
8. Fill every Privacy practices field exactly as documented in `LISTING.md`: mark Web history, User activity and Website content as collected for core functionality, and disclose any administrator-configured network translator. Do not declare analytics, cloud sync or learning effects that do not exist.
9. Use the published `PRIVACY.md` URL and GitHub Issues support URL.
10. Paste the reviewer steps from `LISTING.md`.
11. Save draft, run the dashboard's automated checks, then submit for review.

## Stop conditions

Do not submit if the dashboard reports:

- a permission not present in `extension/manifest.json`;
- remotely hosted code;
- a screenshot size, padding/full-bleed or square-corner error;
- a privacy answer inconsistent with the manifest;
- a ZIP version other than `0.2.6`;
- a package hash different from the checksum attached to the release.

Do not claim that Store approval, one-click install or automatic updates exist until Google returns an approved public listing URL.

## After approval

1. Add the public Store URL to the root README.
2. Install from the Store in a clean Chrome profile.
3. Verify an ordinary signed-in `/watch?v=` page from install to first visible bilingual caption.
4. Verify the optional localhost permission is absent at install and appears only after enabling experimental learning.
5. Record the Store item ID/version and update path in `TEST-RESULTS.md`.
