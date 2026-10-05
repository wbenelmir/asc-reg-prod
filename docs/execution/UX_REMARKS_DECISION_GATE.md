Status: UX_PLAN_DECISIONS_RECORDED
Work package: UX-PLAN (design and decision gate only); owner decisions recorded by UX-PLAN-DECISIONS
Date: 2026-09-29
Previous status: UX_PLAN_READY_FOR_OWNER_DECISION (2026-09-29)
Decisions recorded: 2026-09-29 (see §17)

# UX Remarks Decision Gate — ASC 2026 registration amendments

## 0. Status and scope statement

* This document is a design and decision gate. **No application code was changed.** No source
  file under `apps/`, `config/`, `templates/`, `static/`, `locale/`, `scripts/` or `tests/` was
  created, edited or deleted. The before/after tree manifest proves it (see §15).
* **Phase 4 Prompt 4 was not started.** UX-1, UX-2, UX-3 and UX-4 were not started.
* No migration was created, generated or applied. No dependency was added. No Git, Docker,
  subagent, deployment or external write was used.
* Entry state: Phase 4 Prompt 3 correction 5 is `PASS_READY_FOR_LOCAL_REVIEW_F01_FIXED`. This is
  **not** independently approved. The last independent approval remains Phase 4 Prompt 2.
* Nothing here is a legal approval, an ANPDP approval, a diplomatic-recognition determination or
  production validation. Recommendations below are proposals until the developer or owner records
  a decision in §17.
* Specification files under `docs/specifications/` were read only. Where a remark changes a
  specified requirement, this gate records an amendment in §4. It does not edit the specification.
* **Decisions recorded (2026-09-29).** The owner's decisions are recorded verbatim in §17. The
  UX-PLAN-DECISIONS package changed only this document's header, §0, §16, §17 and §18. UX-1 to
  UX-4 and Phase 4 Prompt 4 were still not started after recording. Items the owner left OPEN
  stay OPEN in §17; nothing is approved by inference.

## 1. Source inventory

### 1.1 Developer remark sources (local only, never packaged)

The originals may contain personal data. They stay outside every archive. This gate records
only their hashes and a neutral English rendering of their content.

| Source | Bytes | SHA-256 | Content | Packaged |
| --- | --- | --- | --- | --- |
| `remarques.docx` | 17624 | `5154d5efc96e81b533ae82ff52a287c07bc0092112ab9aead87e5ddc88fb41b0` | 29 Arabic paragraphs (24 remarks, one heading and continuation lines). No embedded images. | No |
| `input.png` | 203916 | `49de334735539bbc6f2bf29cba38b32641efd77847e0eac7d783bd0a6bf45c24` | English OTP verification page with a single plain code input. **Shows a real email address and a real verification code: personal data.** | No |
| `Placeholder.png` | 46959 | `ba379295ec2f9e480b7c7b25b4e7d95f0317d2e4e002cc9d6b229f6fd9f002f4` | Arabic identity step. Name, date, nationality and residence controls have no placeholder examples. | No |
| `contries.png` | 12158 | `ce398ec9cf76d0205ef460da2714821adefe287c8b3d53a188db6e79d4c32b92` | Arabic identity step. Native date control shows `mm / dd / yyyy`, and the nationality and residence selects are empty. | No |
| `icon.png` | 10243 | `20aeb22d061319a20b19076431ba165084eece98327d594dec36313ec4071e0e` | Browser tab for `/my-passes/` with an Arabic title and no site icon (favicon). | No |
| Arabic-named screenshot (`المؤتمر الافرقي.png`) | 13663 | `2f51992033106dae9fa68afa8fce1fb0f51ea7c553f858baf6848c1972f231a5` | Arabic "My registrations" header. Its subtitle uses the generic wording "فعاليات ASC". | No |

The five images attached to the UX-PLAN request in chat are **byte-identical** to the five
screenshots above (same SHA-256), so they are one source set.

### 1.2 Repository sources inspected (read only)

| Area | Files |
| --- | --- |
| Governance | the project engineering rules, the project status record (historical, kept outside the repository), the completion playbook (historical, kept outside the repository) (§6 M01–M24 matrix, §7 UX packages) |
| Specifications (targeted sections only) | `01_PRD.md` (FR-PER-001..010, FR-IDV-001..008, NFR-L10N-006, NFR-SEC-005, UJ-01); `02_TRD.md` (REG-010, §privacy controller, I18N-006); `03_UI_UX_SPECIFICATION.md` (BL-01, §6.1–6.3, brand favicon line, §15.4); `04_APPLICATION_FLOW.md` (§4, OD-AF-03, OD-AF-05); `05_BACKEND_SCHEMA.md` (§contact rule, controller statement, OD-DATA-03); `06_IMPLEMENTATION_PLAN.md` (mobile rule, Law 18-07 traceability); the project handoff context (historical) (mobile rule, legal baseline) |
| Forms | `apps/registrations/forms/__init__.py`, `apps/accounts/forms/__init__.py`, `apps/invitations/forms/__init__.py` |
| Models | `apps/registrations/models`, `apps/people/models`, `apps/core/models`, `apps/organizations/models`, `apps/events/models`, `apps/privacy/models`, `apps/invitations/models` |
| Services and selectors | `apps/registrations/services` (step saves, completeness guards, snapshot), `apps/people/services` (phone normalization), `apps/events/selectors` (open-event selection), `apps/invitations/services` (invited drafts, claims, delegation columns) |
| Views and URLs | `apps/registrations/views.py`, `apps/registrations/urls.py`, `apps/accounts/urls.py`, `config/urls.py` |
| Templates and assets | `templates/base.html`, `templates/layouts/base_public.html`, `templates/components/picker.html`, `templates/accounts/otp_verify.html`, `static/img/brand/asc-logo.svg` |
| Locales | `locale/ar/LC_MESSAGES/django.po`, `locale/fr/LC_MESSAGES/django.po` (targeted lines) |
| Reference data | `apps/core/management/commands/seed_phase1_local_data.py`, `apps/registrations/migrations/0002_seed_interest_topics.py` |
| Settings | `config/settings/base.py` (OTP throttling, security headers) |
| Finder (licensed, read only) | `reference/finder/assets/vendor/{choices.js,cleave.js,flatpickr}/package.json` (version and licence only) |

### 1.3 Remark paragraph mapping

The `remarques.docx` paragraphs (0-based) map onto the playbook IDs as follows. Paragraph 13 is
the heading "Professional information". Paragraphs 24–25 and 27–28 continue M23 and M24.

| Paragraphs | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 14 | 15 | 16 | 17 | 18 | 19 | 20 | 21 | 22 | 23–25 | 26–28 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ID | M01 | M02 | M03 | M04 | M05 | M06 | M07 | M08 | M09 | M10 | M11 | M12 | M13 | M14 | M15 | M16 | M17 | M18 | M19 | M20 | M21 | M22 | M23 | M24 |

## 2. Recovered state relevant to the remarks

These are facts from the current tree. They define the starting point for every package.

| # | Finding | Evidence |
| --- | --- | --- |
| F-1 | The OTP code is already one text input with `inputmode=numeric`, `autocomplete=one-time-code`, `pattern=[0-9]{6}` and length 6. The problem is its presentation, not its contract. | `apps/accounts/forms/__init__.py:16-29` |
| F-2 | The NIN is already validated as exactly 18 ASCII digits in the form and service, and stored encrypted with a blind index. | `apps/registrations/forms/__init__.py:127-132,185-188`; `services/__init__.py:668` |
| F-3 | Names accept any characters up to 200. No script rule exists. | `forms/__init__.py:111-112` |
| F-4 | The date of birth only has to be in the past. There is no minimum age. Date inputs are native `type=date` with ISO-only parsing, so the browser locale decides the display (`mm/dd/yyyy` in the screenshot). | `forms/__init__.py:113-117,169-173` |
| F-5 | The country catalog is **not** production data. Only Algeria and France are seeded. `Country` has localized names but no nationality labels and no catalog version. | `apps/core/management/commands/seed_phase1_local_data.py:35-36`; `apps/core/models/__init__.py:89-133` |
| F-6 | The mobile number is required only when nationality is Algeria (form, view and completeness guard). The "country code" control is a country list, not a calling-code list. | `views.py:253`; `forms/__init__.py:224-267`; `services/__init__.py:1294-1303` |
| F-7 | The phone normalizer parses with the selected region. An international `+…` paste is therefore accepted as a number of another region while the selector still names the chosen country. There is no number-type check. | `apps/people/services/__init__.py:32-50` |
| F-8 | Website, profile URL, department and biography are all **required** (form and completeness guard). The biography limit is 2000 characters. | `forms/__init__.py:287-312`; `services/__init__.py:1306-1320` |
| F-9 | `OrganizationType` has 6 values and columns of `max_length=16`. The sectors are only `TECH`, and there are only 5 interest topics with no grouping. | `apps/organizations/models/__init__.py:15-26,41,96`; seed files |
| F-10 | "Multinational" cannot be expressed. The organization country is a single required country. | `forms/__init__.py:296-298`; `ProfessionalAffiliation` |
| F-11 | Accessibility needs are one optional free-text field (2000 characters). It is copied into the immutable submission snapshot JSON and is opted into the language-switch `sessionStorage` preservation. | `forms/__init__.py:334-336,349-355`; `services/__init__.py:1481`; `templates/base.html:116-211` |
| F-12 | The legal notices are synthetic drafts marked `[DRAFT -- pending Legal review]`. The model separates acknowledgement, terms and optional marketing. `ConsentPurpose` and `ConsentRecord` exist. | seed `:55-68`; `apps/privacy/models/__init__.py:25-152`; `forms/__init__.py:374-384` |
| F-13 | Public registration is "open" whenever the event status is `REGISTRATION_OPEN`. `EventEdition.registration_opens_at` and `registration_closes_at` exist but **are not enforced anywhere**. Invitation campaigns default to `fallback_mode=OFFER_OPEN_REGISTRATION`. | `apps/events/selectors/__init__.py:22-37`; `apps/events/models/__init__.py:35-36`; `apps/invitations/models/__init__.py:39-41,70-74` |
| F-14 | There is no CAPTCHA. OTP issuance has recipient and network throttles protected by advisory locks (ADR-0007). | `config/settings/base.py:436-451` |
| F-15 | `<head>` has only charset, viewport and title. There is no favicon, description, robots directive, canonical or hreflang link, or social metadata. No `X-Robots-Tag` or `robots.txt` exists. | `templates/base.html:7-28`; project-wide search |
| F-16 | The public footer shows two plain text labels and no links. | `templates/layouts/base_public.html:63-70` |
| F-17 | The Arabic catalog uses the generic "فعاليات ASC" wording and leaves "African Startup Conference" untranslated in Arabic. | `locale/ar/LC_MESSAGES/django.po:7393, 5894-5895` |
| F-18 | A project-owned filterable picker (F8) already exists. It adds a filter box above a native select and needs no dependency. | `templates/components/picker.html` |
| F-19 | Finder ships Choices.js 11.0.2 (MIT), Cleave.js 1.6.0 (Apache-2.0) and flatpickr 4.6.13 (MIT) with `ar` and `fr` locales. None is used by the project today. | `reference/finder/assets/vendor/*/package.json` |
| F-20 | The delegation CSV import carries `given_names` and `family_name`. On-behalf creation carries the participant email only. | `apps/invitations/services/__init__.py:753`; `apps/invitations/forms/__init__.py:64-65` |

## 3. Remark traceability matrix M01–M24

Package codes: U1 = UX-1, U2 = UX-2, U3 = UX-3, U4 = UX-4. Decision references point to §5. A
remark marked "safe" can be implemented once this gate is accepted. The others wait for the
listed decision.

| ID | Remark (English rendering) | Screenshot | Current state | Required outcome | Affected surfaces | Supersedes | Pkg | Decisions |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| M01 | Add an open-source CAPTCHA to registration, customized to the template colours. | — | F-14 | Self-hostable open-source challenge on public abuse paths, themed with Finder tokens, as a supplement to throttling. | `accounts` OTP request view and form, `otp_request.html`, final submit, settings, CSP, a new replay store, locales | — (supports NFR-SEC-005) | U4 | D-13, C-10, S-16 |
| M02 | Use formatted inputs (see input.png). | input.png | F-1 | OTP shown as six visual cells in **one** accessible input. Format hints and masks for NIN, phone and dates that never alter stored values. | `OtpVerifyForm`, `otp_verify.html`, `asc-ui.css`, `asc-enhance.js` | — | U1 | S-01, S-02 |
| M03 | Format the national identity number (18 digits). | — | F-2 | Exactly 18 digits kept as a string. Pasted separators stripped and Arabic-Indic digits normalized. LTR display with a live digit count. Encryption and blind index unchanged. | `IdentityStepForm`, `save_identity_step`, `step_identity.html` | — (FR-IDV-002 kept) | U2 (widget in U1) | S-03 |
| M04 | Name the event in Arabic "المؤتمر الإفريقي للمؤسسات الناشئة ASC" instead of "فعاليات ASC" on every Arabic page. | Arabic-named screenshot | F-17 | Exact owner-provided Arabic name in Arabic UI, titles, emails and printable surfaces. EN/FR proper names unchanged. | `locale/ar/.../django.po`, email templates, pass print, page titles | — | U1 | S-04, D-03 |
| M05 | Improve the `<head>` and SEO; there is no conference icon. | icon.png | F-15 | Favicon set derived from the authorized logo, localized titles and descriptions for public pages, and noindex/no-cache protection for every private page. | `base.html`, a new robots route, a response-header middleware, `static/img/brand/` | — | U1 | S-05, D-04 |
| M06 | Add placeholders to every input, following the active language. | Placeholder.png | no placeholders | Localized, synthetic format examples on suitable text controls. Visible labels kept. None on select, date-part, checkbox or file controls. | all step forms, `components/field.html`, 3 locales | — | U1 (U2 for new fields) | S-06 |
| M07 | Nationality and country lists: only countries recognized by Algeria; remove Israel and Kosovo. | contries.png | F-5 | Versioned catalog with stable codes, EN/FR/AR country and nationality labels, owner-approved recognition policy, the two named entries excluded from selection and history kept. | `core.Country`, a catalog import, all country selects | — | U2 | C-01, C-02, D-05 |
| M08 | Default to Algeria / Algerian. | contries.png | empty select | Algeria preselected for nationality and residence **only** on genuinely blank new forms. Editable, and never overwriting saved answers. | `identity_step` view, initial data | — | U2 | S-09 |
| M09 | Every select, especially long lists, should support search-and-select. | contries.png | F-18 (partial) | Reusable accessible combobox over a native select, with server-side choice validation unchanged. | `components/`, `asc-enhance.js`, every long select | — | U1 (component), U2 (adoption) | S-07 |
| M10 | Date format should follow the language, e.g. French dd/mm/yyyy. | contries.png | F-4 | Day / Month / Year entry independent of the browser locale, ISO storage and accessible errors. | `IdentityStepForm` date fields, a new widget | — | U1 (widget), U2 (rules) | S-08 |
| M11 | Given and family names must always be in Latin letters, whatever the UI language. | Placeholder.png | F-3 | Unicode Latin-script validation (accents allowed) in all locales, no transliteration, and legacy values preserved. | `IdentityStepForm`, `save_identity_step`, delegation import, on-behalf claim completion | UI/UX §6.3 "Names" (partly); FR-PER-002 deferred | U2 | D-02, S-14 |
| M12 | Date of birth: no children; apply the necessary validation. | — | F-4 | Owner-approved minimum age measured on a defined reference date, exact boundaries, and defined invited-channel behaviour. | identity form and service, completeness guard, `EventEdition` | new eligibility rule | U2 | D-09 |
| M13 | Contact information mandatory for everyone (Algerian or not); better phone UX; link country to number; fix RTL; the user must not type the country code, or derive it from the country. | — | F-6, F-7 | Verified email (already) plus phone required for all **new** registrations, a calling-code selector defaulted from residence, E.164 normalization, LTR number inside RTL, and clear guidance. | `ContactStepForm`, `contact_step` view, `_require_complete_contact`, `normalize_mobile_e164`, `step_contact.html` | FR-PER-005, REG-010, BL-01, UI/UX §6.3 "Phone", AF §4 line 270, OD-AF-03, OD-DATA-03, Schema contact rule, Impl-plan line 386, Handoff 155-156 | U2 | D-01, S-10 |
| M14 | Professional organization: one input language, a more global organization-type list, searchable. | — | F-9 | Latin-script organization text (subject to D-06), an expanded stable-code type taxonomy with localized labels, searchable. | `ProfessionalStepForm`, `OrganizationType`, `Organization`, `ProfessionalAffiliation` | NFR-L10N-006 (organization names) if D-06 = Latin only | U2 | D-06, C-04 |
| M15 | Sector list: global and expanded. | — | F-9 | Stable-code international sector taxonomy with EN/FR/AR labels. | `core.Sector`, catalog import | — | U2 | C-05 |
| M16 | Organization country: consider "multinational"; it may not be one country. | — | F-10 | Headquarters country kept as a real country, plus a separate operating-scope field. No fake "multinational" country. | `ProfessionalAffiliation`, form, snapshot | — | U2 | D-07 |
| M17 | Website and profile URL optional; normalize the format; do not make the UX harder. | — | F-8 | Optional fields, `https://` added when the scheme is missing, HTTP(S) only, no credentials, never fetched. | `ProfessionalStepForm`, `_require_complete_professional` | current implementation only | U2 | S-11 |
| M18 | Short biography: control and show the size in the form; any language. | — | F-8 | Visible limit and live counter that match server validation; free language. | form, template, service | — | U2 | D-08, S-12 |
| M19 | Job title and department: one language, Latin characters only. | — | F-8 | Latin-script text rule with digits and limited punctuation. | form, service | — | U2 | S-14, D-08 (department requiredness) |
| M20 | Interests: expand the list; if large, change the UX. | — | F-9 | Grouped taxonomy with a searchable multi-select, chips and keyboard removal. | `InterestTopic`, `InterestsStepForm`, template | — | U2 | C-06 |
| M21 | Record special needs or disability; use a global standard. | — | F-11 | Optional accommodation request with practical support categories and "prefer not to say". No diagnosis. Minimal, restricted and encrypted. | new model, `InterestsStepForm` or its own step, snapshot, ops views, exports | replaces the free-text-only design | U3 | D-10, C-08, S-18 |
| M22 | Privacy notice and terms: use an international standard without a review loop; add consent to data processing per the national personal-data protection authority (ANPDP). | — | F-12 | Complete EN/FR/AR drafts, separate acknowledgement, terms and explicit processing consent, versioned evidence and no approval claim. | `privacy` app, `NoticesForm`, legal-version data, `step_notices.html` | none (spec already requires legal validation) | U3 | D-11, C-09 |
| M23 | Link the footer to the official conference site (contact info, follow us): https://africanstartupconference.org/ and the ministry https://mkesm.gov.dz/. | — | F-16 | Footer links to the two owner-authorized URLs. Contact and social items only from verified sources. | all layout footers, locales | — | U1 | S-13, C-07 |
| M24 | Open and close registration at will. After the period ends, registration closes even with the link. Close the public flow but keep restricted invitation sending. | — | F-13 | Per-event server-side channel policy enforced on GET, POST and final submit, with invitation-only mode, an audited authorized control and window enforcement. | `EventEdition`, `events` selectors and policies, `registrations` views and services, `invitations` services, `accounts` OTP view | — (schema lists "registration windows") | U4 | D-12, S-17 |

## 4. Amendment ledger (explicit supersession, no spec edits)

| Amendment | Remark | Supersedes or affects | Nature | Resolution proposed |
| --- | --- | --- | --- | --- |
| A-01 | M13 | PRD FR-PER-005; TRD REG-010; UI/UX BL-01 and §6.3 Phone; Application Flow §4 (line 270) and OD-AF-03; Schema contact rule (line 257) and OD-DATA-03; Implementation Plan (line 386); Handoff (lines 155-156) | Every source allows "unless an approved operational policy requires it". Once approved (D-01), the remark **is** that operational policy. | Not a silent conflict. Record the approval as the operational policy, and leave the historical specs unedited. |
| A-02 | M11 | UI/UX §6.3 Names ("Accept Unicode … do not impose English-only patterns"); PRD FR-PER-002 (Should: optional native-language names); PRD NFR-L10N-006 | **Material conflict.** Latin-only input narrows "accept Unicode". The rule is still not English-only, because accents and combining marks stay allowed. | D-02: Latin mandatory. Native-script names deferred (FR-PER-002 is "Should"). NFR-L10N-006 still holds because the submitted form is preserved and never machine-translated. |
| A-03 | M14 | PRD NFR-L10N-006 (organization names preserve submitted Arabic and Latin forms) | Conflict only if organization names become Latin-only. | D-06. |
| A-04 | M17, M19, M18 | Current implementation (required website, profile URL and department; 2000-character biography) | Implementation change. No specification requires these values. | S-11, D-08. |
| A-05 | M12 | PRD FR-PER-003 (date of birth "where required") | New eligibility rule, not in the specifications. | D-09. |
| A-06 | M21 | Current `accessibility_needs_text` design | Data-model change for sensitive data. | D-10. |
| A-07 | M24 | Application Flow OD-AF-05 (invalid-invitation fallback offers open registration) | Fallback must be suppressed while public registration is not open. | D-12. |
| A-08 | M04 | Arabic translations only | Copy change. | S-04, D-03. |
| A-09 | M01 | New control supporting PRD NFR-SEC-005 | Addition. | D-13. |

## 5. Decisions

### 5.1 Approved / safe decisions

The remark itself authorizes these, or they are the only safe interpretation. The developer can
object to any of them in the decision record. Otherwise they apply once this gate is accepted.

| ID | Decision |
| --- | --- |
| S-01 | **OTP (M02).** One logical `<input>`: 6 digits, `type=text`, `inputmode=numeric`, `autocomplete=one-time-code`, `dir=ltr`, `maxlength` large enough to accept a pasted code with spaces. Six visual cells are drawn with CSS on that single input. No six separate inputs and no auto-submit (WCAG 3.2.2). Paste and SMS/email autofill fill all cells. Server contract unchanged: exactly 6 ASCII digits after S-02 normalization. |
| S-02 | **Digit normalization.** OTP, NIN, phone and date parts accept ASCII digits, Arabic-Indic digits (U+0660–U+0669) and Extended Arabic-Indic digits (U+06F0–U+06F9). The server converts them to ASCII before validating. Any other character, apart from the separators S-03 and S-10 allow, is rejected. Stored values are ASCII digits only. |
| S-03 | **NIN (M03).** Strip U+0020, U+00A0, U+2009, U+202F and `-`, apply S-02, then require `^[0-9]{18}$`. Keep it as a string and never convert it to a number, so leading zeros survive. No checksum is invented. The input is LTR and monospace with a live "n / 18" counter. No fixed grouping mask unless the owner supplies the printed grouping. The blind-index input stays the same canonical 18-digit string, so no reindex is needed. Passport numbers never receive the NIN mask. |
| S-04 | **Arabic event name (M04).** The exact string is `المؤتمر الإفريقي للمؤسسات الناشئة ASC`, taken code point by code point from `remarques.docx`, with hamza-under-alef in "الإفريقي". It replaces the generic "فعاليات ASC" wording and translates "African Startup Conference" in the Arabic catalog, inside `<bdi>` where "ASC" sits next to Arabic text. Historical evidence and reports are not rewritten. |
| S-05 | **Private-page protection (M05).** Every non-public response carries `X-Robots-Tag: noindex, nofollow` and `<meta name="robots" content="noindex, nofollow">`. Authenticated, token, invitation, claim, pass, export and entry pages also keep `Cache-Control: private, no-store`. A `robots.txt` allows only the public landing path. No sitemap lists private URLs. No analytics, tracking pixels or third-party metadata widgets. `canonical`, `hreflang` and Open Graph are emitted only when a configured public base URL exists; none is invented. |
| S-06 | **Placeholders (M06).** Placeholders are localized, synthetic format examples (§6.7). They never replace a label or helper text, and they are never put on select, date-part, radio, checkbox or file controls. Placeholder colour must meet at least 4.5:1 contrast on the field background. |
| S-07 | **Searchable selects (M09).** A project-owned progressive-enhancement combobox built on the existing F8 picker pattern (WAI-ARIA 1.2 select-only combobox with a filterable listbox). With JavaScript off, the native `<select>` still works. No new dependency. The server still validates the submitted value against the same queryset. *Alternative considered:* Finder's Choices.js 11.0.2 (MIT). Rejected by default because it adds a dependency and its accessibility would need separate verification. |
| S-08 | **Date entry (M10).** Date of birth and passport expiry use three labelled numeric fields: Day, Month, Year. This follows the memorable-date pattern and does not depend on the browser locale. The order is Day, Month, Year in all three languages, laid out right to left in Arabic, with LTR digits in each field. The server combines the parts into an ISO `date` and rejects impossible dates such as 31/02. Storage stays ISO. flatpickr is not used. The developer may ask for Month/Day/Year in English only. |
| S-09 | **Defaults (M08).** Nationality and residence start as Algeria only when the profile has no saved value **and** no bound POST exists. The default never overwrites a draft, a language-switch restore or an invited prefill. It never forces the NIN path; the path still follows the existing nationality rule. |
| S-10 | **Phone UX (M13).** The calling-code selector is searchable, labelled like "Algeria (+213)" and built from the country catalog plus `phonenumbers` metadata, an already approved dependency. It defaults from country of residence **only while the phone field is empty**. The number field accepts a national number (with or without the trunk `0`) or a full international paste starting with `+` or `00`. On such a paste, the region comes from the number, and the selector changes visibly with an announced status. Shared calling codes such as +1, +7 and +262 keep the selector region when valid for it. The server stores E.164 and the region derived from the parsed number, which closes F-7. The number is displayed LTR in `<bdi dir="ltr">` and the prefix adorner sits before the number in LTR order. Helper text: "Do not type the country code; it is added from your selection. You can also paste a full number starting with +." Format validity is never treated as proof of ownership. No SMS verification is added. |
| S-11 | **URLs (M17).** Optional. Trim the value, add `https://` when no scheme is present, then require `http` or `https`. Lowercase the scheme and host, validate the IDN host through IDNA and keep the path and query unchanged. Reject userinfo, `javascript:`, `data:`, `file:` and other schemes, and anything over 300 characters. Links are never fetched, previewed or embedded, and they render with `rel="noopener noreferrer nofollow"`. No domain allow-list. |
| S-12 | **Counters (M18 and all long text).** Normalize CR LF to LF and apply NFC before counting code points, on the client and on the server, so the counter and the server limit agree. The counter is announced politely at 80% and 100% of the limit and never blocks typing silently. |
| S-13 | **Footer (M23).** Link to `https://africanstartupconference.org/` and `https://mkesm.gov.dz/`, both authorized by the developer in the remark, with localized visible names and an external-link indicator. Contact and social items appear only after verification (C-07). Unresolved items are omitted, never guessed. |
| S-14 | **Legacy data.** Records that already exist are never bulk-transliterated, rewritten, invalidated or purged to fit the new rules. New rules apply to new input and to edits of the affected field. An edit form shows a nonconforming legacy value with an inline correction request instead of silently clearing it. Login, passes and badges never depend on rewriting historical identity. |
| S-15 | **Neutral registration.** No new field lets a registrant choose a role, badge type, access level or privilege. An accommodation request, for example for a companion, is an operational request for authorized staff. It never assigns a badge. |
| S-16 | **CAPTCHA constraints (M01).** Open source and self-hosted only. No proprietary or external-telemetry service (such as reCAPTCHA, hCaptcha or Turnstile) and no home-made visual puzzle. Verification is server-side and bound to action and expiry, with one-time consumption. It supplements the existing throttles and never replaces them. Failure is closed, with a documented authorized recovery switch and no silent global fail-open. |
| S-17 | **Channel enforcement (M24).** Closure is enforced on the server for direct GET, forged POST and stale-tab final submissions. Login, passes, the workspace and authorized operational work continue. Closure is reversible and never deletes registrations or drafts. Channel changes are permission-controlled and audited. |
| S-18 | **Sensitive accommodation data (M21).** It is never placed in QR payloads, offline packages, general registration lists, notification emails, logs, search indexes, telemetry or the language-switch `sessionStorage`. |

### 5.2 Decisions requiring owner or legal confirmation

Each item gives a recommendation. Unanswered items block only their own feature.

| ID | Question | Recommended choice | Alternatives | Blocks |
| --- | --- | --- | --- | --- |
| D-01 | Phone required for **all** new registrations (A-01)? | Yes, on every participant-completed channel (open, invitation, delegation claim, on-behalf claim) at final submission. Already submitted records are not forced. | Required only for self-service open registration. | M13 (U2) |
| D-02 | Latin-only personal names, and the FR-PER-002 native-script names (A-02)? | Latin mandatory in all locales, with accents and combining marks allowed. Helper text: "as written in Latin letters in your passport or on your identity card". No Arabic-script name field in V1; FR-PER-002 recorded as deferred. | Latin mandatory **plus** optional Arabic-script given and family names (additive migration). | M11 (U2) |
| D-03 | Scope of the Arabic event name. | Full name in headings, footers, emails, printable pass, metadata description and legal text. The short form "ASC 2026" is allowed only in compact contexts (tab-title suffix, chips). Keep "2026" where the edition matters. | Full name everywhere, including tab titles. | M04 final copy (U1) |
| D-04 | Favicon source. | Derive a compact mark from the graphic (stripes and Africa) part of `static/img/brand/asc-logo.svg`, without the wordmark. SVG plus 32 px and 180 px PNG are produced by deterministic tooling. The developer approves the rendered mark visually. | The developer supplies an official compact mark. | M05 icon (U1) |
| D-05 | Nationality option labels. | Show **country names** under the label "Nationality" (for example "الجزائر", "Algérie"), avoiding gendered demonyms in AR and FR. | Demonym labels (for example "جزائري/جزائرية", "Algérien(ne)") from the catalog. | M07, M08 (U2) |
| D-06 | Organization name script. | Latin script required for the organization name, the same rule as job title (digits and limited punctuation allowed). Hint: "Use the organization's official Latin-script name (French or English)." | Any script for the organization name, with Latin required only for personal fields. | M14 (U2) |
| D-07 | Multinational organizations. | Keep a required **headquarters country** (a real country), plus a required `operating_scope` of National / Regional (several countries) / Multinational or global. No list of operating countries. | Also an optional multi-select of operating countries (extra table). | M16 (U2) |
| D-08 | Professional lengths and requiredness. | Biography **optional**, max **500** characters. Department **optional**. Job title required, max 120. Organization name required, max 200 in the form (the database stays 300). | Biography required; 1000 characters; department required. | M17–M19 (U2) |
| D-09 | Minimum age. | **18 full years on the event's first local day** (`EventEdition.starts_at` in the event timezone). One threshold for every channel, with no invitation exception by default. If the event date is missing, fail closed with a configuration error, never "no check". This is not a claim about Algeria's legal age of majority. | Another organizer-defined threshold. A documented exception for specific invitation campaigns, for example a student delegation, enabled by an authorized operator with a reason. | M12 (U2) |
| D-10 | Accommodation (disability) data design. | A voluntary question with No / Yes / Prefer not to say. If Yes: practical support categories (§6.5) plus an optional note of up to **300** characters with a warning not to include medical details. No diagnosis, percentage, certificate or clinical questionnaire. The model is informed by the UN CRPD social model and "reasonable accommodation"; the Washington Group questions are rejected because they are a statistical measurement tool, not an accommodation request. The note is encrypted at rest. Visibility is limited to a dedicated support-coordination permission. Existing free text becomes legacy, hidden from general views and kept (not purged) until the retention decision. | No free-text note. Or keep the current free text only. | M21 (U3) |
| D-11 | Legal notices and processing consent. | Controller: the Ministry of Knowledge Economy, Startups and Micro-enterprises, as stated in the specifications (confirm the official names in 3 languages). Three separate required statements: privacy-notice acknowledgement, registration terms, and explicit consent to personal-data processing under Law 18-07 as amended by Law 25-11. A fourth, separate explicit consent appears only when accommodation data is provided. Marketing stays optional and unticked. Drafts are complete; the institutional facts in C-09 are release blockers, not invented. | Legal basis other than consent for core processing, with consent limited to the optional and sensitive purposes, if the legal function decides so. | M22 (U3) |
| D-12 | Registration channel modes and precedence. | Per event: `public_registration_mode` with OPEN / INVITATION_ONLY / CLOSED. Public creation is allowed only when the mode is OPEN **and** the time is within `registration_opens_at`…`registration_closes_at` (when set); the most restrictive state wins. INVITATION_ONLY: valid campaign links, delegation claims and on-behalf claims continue under their own validity, capacity and expiry, and the invitation `fallback_mode` to open registration is suppressed. CLOSED: no participant-initiated draft creation or submission on any channel; authorized staff can still review, issue passes and run entry. Public drafts that exist at closure **cannot be submitted** and stay stored. Channel control needs a scoped permission and a mandatory reason, and it is audited. | A grace period for drafts started before closure. On-behalf creation still allowed in CLOSED for authorized staff. | M24 (U4) |
| D-13 | CAPTCHA product and placement. | UX-4 evaluates candidates (C-10) and records the choice before adding a dependency. Leading candidate for evaluation: ALTCHA (MIT, self-hosted proof-of-work, local HMAC verification). Placement: the public OTP request form, the main email-bombing vector, for every visitor, invited or not, since it is the same start page. No challenge on OTP verification or final submit unless the evaluation shows a need. | mCaptcha (AGPL-3.0, separate server). Cap (verify licence). | M01 (U4) |
| D-14 | Phone number type. | Accept numbers whose type is MOBILE or FIXED_LINE_OR_MOBILE, because the label says "Mobile number". | Accept any valid number type. | M13 (U2), low impact |
| D-15 | Workflow record. | Record the temporary single-reviewer workflow (playbook §2) as **proposed** for developer progression. An implementation-side review is not an independent approval. | — | Process only |

### 5.3 Decisions requiring an authoritative catalog or source

| ID | Needed | Proposed source / approach | Owner action |
| --- | --- | --- | --- |
| C-01 | Country catalog and Algerian recognition policy (M07) | ISO 3166-1 alpha-2 as the technical code base (ISO codes are not proof of recognition). A versioned, reviewed file in the repository with a cutoff date. The two remark exclusions are authorized: Israel (IL) is excluded; Kosovo (XK) is a user-assigned code and not part of ISO 3166-1, so it is simply not added. Owner decisions needed for: Western Sahara / SADR label (EH), Palestine label (PS, include), Taiwan (TW), dependent territories (for example PR, HK, MO, GI, NC, RE), and the "stateless / not listed" assistance path. | Provide an official list, for example from the Ministry of Foreign Affairs, or approve the proposed list line by line **once**. |
| C-02 | EN/FR/AR country names (and nationality labels if D-05 is changed) | Unicode CLDR, at a pinned recorded version, as a translation base, reviewed by a native Arabic and French reviewer, with owner overrides recorded in the catalog file. | Approve the reviewed catalog. |
| C-03 | Calling codes and number metadata | `phonenumbers`, already an approved dependency, pinned in `uv.lock`. No new source is needed. | None (resolved). |
| C-04 | Organization-type taxonomy (M14) | The proposed list is in §6.6, with stable codes of at most 16 characters so no column change is needed. Existing codes (MINISTRY, PUBLIC_BODY, COMPANY, STARTUP, INSTITUTION, OTHER) are kept. | Approve or edit the list. |
| C-05 | Sector taxonomy (M15) | A startup-ecosystem vertical list (§6.6), each vertical mapped to an ISIC Rev. 5 section (United Nations Statistics Division) for reporting. The ISIC mapping is verified from the official UN source in UX-2. | Approve the list. |
| C-06 | Interest topics with groups (M20) | The grouped list is in §6.6. The existing 5 codes are kept. | Approve the list. |
| C-07 | Footer contact and social links, and the Ministry's official names in AR/FR/EN (M23) | Taken from the two official sites or from developer-provided values. The playbook reports that the conference homepage served a JavaScript shell and the ministry site was unreachable during its preparation, so nothing was extracted. | Provide the email, phone, postal city and social handles, or approve their verification in UX-1. |
| C-08 | Accommodation categories (M21) | The CRPD-informed practical categories are in §6.5. Algerian Sign Language versus International Sign interpretation needs the owner's naming. | Approve the categories. |
| C-09 | Legal institutional facts (M22) | Controller legal identity and address; data-protection contact channel; ANPDP declaration or authorization reference, if one applies; purposes; recipients (for example security services, badge producer, email provider); hosting location and any cross-border transfers; retention periods per category (OD-DATA-09, OD-AF-21); rights-request route. Official legal starting points are recorded in playbook §10 and are re-verified in UX-3. | Provide the facts or name the responsible legal contact. |
| C-10 | CAPTCHA candidate documentation (M01) | For each candidate: official repository, licence, maintenance activity, threat model, accessibility, AR/FR localization, CSP needs (script, worker), low-end-device cost, and server-library availability for Python. | None until UX-4. The evaluation is part of UX-4. |

## 6. Field-by-field contract (proposed, server-authoritative)

Frontend formatting is only a convenience. The server re-validates every rule. "Latin rule"
means L-NAME or L-TEXT from §7.

### 6.1 Sign-in

| Field | Required | Input and script | Normalization | Length | Storage | Server validation | Helper text / error (EN) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Email (OTP request) | Yes | email, LTR | trim, lowercase domain (existing) | ≤ 254 | existing ContactPoint | existing | unchanged |
| OTP code | Yes | 6 digits, one input (S-01) | S-02, strip spaces | 6 | not stored as plaintext (existing) | exactly 6 ASCII digits | "Enter the 6-digit code sent to your email." / "Enter the 6-digit code using digits only." |

### 6.2 Identity step

| Field | Required | Script / format | Normalization | Length | Storage | Server validation | Helper / error (EN) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Given name(s) | Yes | L-NAME (D-02) | NFC, trim, collapse spaces, typographic apostrophes (U+2019, U+02BC) → U+0027 | 1–100 (database 200 kept) | `submitted_given_names` | L-NAME, at least 1 letter | "In Latin letters, exactly as in your passport or on the Latin side of your identity card." / "Use Latin letters only (accents are allowed)." |
| Family name | Yes | L-NAME | same | 1–100 | `submitted_family_name` | same | same |
| Date of birth | Yes | Day / Month / Year (S-08) | S-02, combine to ISO | — | `date_of_birth` | a real date; in the past; age ≥ threshold on the reference date (D-09); age ≤ 120 | "Day, month and year, for example 07 03 1990." / "You must be at least {n} years old on {date} to register." / "Enter a real date." |
| Nationality | Yes | searchable (S-07) | — | — | FK `Country` | active and selectable catalog entry | default DZ per S-09 |
| Country of residence | Yes | searchable | — | — | FK `Country` | same | default DZ per S-09 |
| Identity path | Yes | radio | — | — | existing | existing nationality rule | unchanged |
| NIN | When NIN path | digits, LTR (S-03) | strip separators, S-02 | exactly 18 | encrypted plus blind index (existing) | `^[0-9]{18}$` | "18 digits, as printed on your national identity card." / "Enter exactly 18 digits." |
| Passport number | When passport path | letters and digits, LTR | trim, remove spaces, uppercase ASCII letters | ≤ 40 (existing) | encrypted plus blind index (existing) | existing, no country-specific pattern | "As printed on the passport data page." |
| Issuing country | When passport path | searchable | — | — | FK | selectable entry | default = nationality when blank |
| Passport expiry | When passport path | Day / Month / Year | as for date of birth | — | `expires_at` | a real date in the future | "Enter a passport expiry date in the future." |

### 6.3 Contact step

| Field | Required | Format | Normalization | Storage | Server validation | Helper / error (EN) |
| --- | --- | --- | --- | --- | --- | --- |
| Verified email | Yes (already) | read-only display | — | existing | existing | — |
| Calling code | Yes (D-01) | searchable "Country (+code)" (S-10) | — | region of the parsed number | selectable entry | default from residence while the phone is empty |
| Mobile number | Yes (D-01) | national or `+` international, LTR | S-02, drop spaces, dots, hyphens and parentheses | E.164 in ContactPoint (existing hash and mask) | `phonenumbers` valid, type per D-14 | "Do not type the country code; it is added from your selection. You can also paste a full number starting with +." / "Enter a valid mobile number for {country}." |

### 6.4 Professional step

| Field | Required | Script | Normalization | Length | Server validation | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Organization name | Yes | L-TEXT (D-06) | NFC, trim, collapse spaces | ≤ 200 (database 300) | L-TEXT, at least 1 letter | independent of invitation provenance; never replaces `Organization` membership |
| Organization type | Yes | searchable (C-04) | — | code ≤ 16 | active code | legacy `INSTITUTION` kept readable |
| Job title | Yes | L-TEXT | same | ≤ 120 | L-TEXT | M19 |
| Department | No (D-08) | L-TEXT | same | ≤ 120 | L-TEXT when present | M19 |
| Sector | Yes | searchable (C-05) | — | — | active code | — |
| Headquarters country | Yes | searchable | — | — | selectable entry | relabel of `country_code` (D-07) |
| Operating scope | Yes (D-07) | radio: National / Regional / Multinational | — | enum | enum | new field |
| Organization website | No | URL (S-11) | S-11 | ≤ 300 | S-11 | placeholder `https://www.example.org` |
| Professional profile URL | No | URL (S-11) | S-11 | ≤ 300 | S-11 | any domain |
| Short biography | No (D-08) | any language | S-12 | ≤ 500 code points (D-08) | same count | live counter; database 2000 kept, legacy longer values preserved (§12) |
| Profile photograph | unchanged | — | — | — | existing | out of scope |

### 6.5 Interests and accommodation step

| Field | Required | Format | Storage | Server validation | Notes |
| --- | --- | --- | --- | --- | --- |
| Areas of interest | Yes, ≥ 1 | grouped searchable multi-select with chips; checkboxes without JavaScript | `RegistrationInterest` (existing) | active topics of the event | C-06 |
| Objectives | unchanged (required) | textarea with counter (S-12) | existing | existing 2000 | counter only |
| Accessibility support needed? | No (voluntary) | radio: No / Yes / Prefer not to say | new model (§12) | enum | D-10; never preserved in `sessionStorage` |
| Support categories | only if "Yes", optional | checkboxes | new model | active category codes | proposed codes: `STEP_FREE_ACCESS` (wheelchair or step-free access), `ACCESSIBLE_SEATING`, `SIGN_LANGUAGE` (interpretation; naming per C-08), `CAPTIONING`, `ACCESSIBLE_DOCUMENTS` (large print or screen-reader-friendly), `ASSISTANCE_ON_SITE`, `COMPANION_ACCESS` (a personal assistant accompanies me; staff decide), `SERVICE_ANIMAL`, `QUIET_SPACE`, `OTHER` |
| Note | No | textarea, ≤ 300 | encrypted | length, S-12 | "Do not include medical details or diagnoses." |

### 6.6 Proposed reference-list contents (for C-04 to C-06 approval)

* **Organization types**:
  * existing codes: MINISTRY, PUBLIC_BODY, COMPANY, STARTUP, INSTITUTION (legacy, hidden from new
    selection) and OTHER;
  * added codes: LOCAL_GOV (local or regional government), INTL_ORG (international or
    intergovernmental organization), DIPLOMATIC (embassy or diplomatic mission), SME (small or
    medium enterprise), INCUBATOR (incubator, accelerator or tech park), INVESTOR (venture
    capital, angel or fund), FINANCIAL (bank or financial institution), UNIVERSITY (university or
    higher education), RESEARCH (research centre), NGO (non-profit, NGO or association),
    BUSINESS_ASSOC (chamber, federation or professional association), MEDIA (media or press),
    FREELANCE (independent or self-employed) and STUDENT (student, no organization).
* **Sectors**:
  * startup-ecosystem verticals: AGRITECH, CLEANTECH (energy and climate), EDTECH, FINTECH,
    HEALTHTECH, AI_DATA, CYBERSECURITY, ECOMMERCE, LOGISTICS, MOBILITY, INDUSTRY (manufacturing
    and Industry 4.0), TOURISM, MEDIA_CREATIVE, TELECOM, SOFTWARE (software and SaaS), PROPTECH
    (construction and real estate), GOVTECH, WATER (water and environment), BIOTECH, SPACE,
    PUBLIC_SECTOR, CONSULTING, FINANCE_INVEST and OTHER;
  * `TECH` is kept;
  * each code maps to an ISIC Rev. 5 section.
* **Interest groups**:
  * Funding and investment: FUNDING, VENTURE_CAPITAL, PUBLIC_FUNDING;
  * Growth: MENTORSHIP, MARKET_ACCESS, EXPORT_AFRICA, PARTNERSHIPS;
  * Innovation: TECHNOLOGY, AI, OPEN_INNOVATION, RESEARCH_TRANSFER;
  * Ecosystem and policy: POLICY, STARTUP_LABEL, INCUBATION;
  * Talent: RECRUITMENT, SKILLS;
  * Networking: B2B_MEETINGS, INVESTOR_MEETINGS;
  * existing codes are kept.

### 6.7 Proposed placeholder examples (synthetic)

| Field | EN | FR | AR |
| --- | --- | --- | --- |
| Given name(s) | e.g. Amina | ex. Amina | مثال: Amina |
| Family name | e.g. Benali | ex. Benali | مثال: Benali |
| NIN | 18 digits | 18 chiffres | 18 رقمًا |
| Passport number | e.g. AB1234567 | ex. AB1234567 | مثال: AB1234567 |
| Mobile number | the national example for the selected region, generated from `phonenumbers` example metadata | same | same |
| Organization name | e.g. Example Technologies SARL | ex. Example Technologies SARL | مثال: Example Technologies SARL |
| Job title | e.g. Product Manager | ex. Chef de produit | مثال: Product Manager |
| Department | e.g. Research and Development | ex. Recherche et développement | مثال: Research and Development |
| Website / profile | https://www.example.org | https://www.example.org | https://www.example.org |
| Biography / objectives | a short prompt sentence, no personal example | same | same |

Arabic placeholders for Latin-only fields deliberately show Latin examples, so the script rule
is visible. The Latin part is wrapped as LTR (`<bdi dir="ltr">` equivalent through `dir="auto"`
on the input).

## 7. Proposed validation rules (authoritative server side)

* **L-NAME, Latin personal name.**
  1. Apply NFC.
  2. Collapse whitespace runs to one U+0020 and trim.
  3. Map U+2019 and U+02BC to U+0027.
  4. Accept a character when it is:
     * a letter (Unicode category L*) whose `unicodedata.name()` starts with `LATIN `;
     * a combining mark (category Mn) that directly follows an accepted letter;
     * U+0020, U+002D (hyphen) or U+0027 (apostrophe), neither leading nor trailing, and never
       doubled.
  5. Require at least one letter.

  This uses the standard library only (no `regex` dependency). It is not ASCII-only: "Zoé",
  "Nuñez", "Ǧamal" and "O'Neil" pass; Arabic, Cyrillic, digits and emoji fail.
* **L-TEXT, Latin professional text.** The L-NAME letter rule, plus ASCII digits and
  `. , & / ( ) + # : ; ' - "` and U+0020. It needs at least one Latin letter. It applies to the
  organization name (D-06), job title and department.
* **Digits.** S-02 applies to OTP, NIN, phone and date parts only. No other field converts digits.
* **Age (D-09).**
  1. Take the reference date as the local calendar date of `EventEdition.starts_at` in
     `EventEdition.timezone`.
  2. `age = ref.year - dob.year - ((ref.month, ref.day) < (dob.month, dob.day))`.
  3. Someone born on 29 February reaches their birthday on 1 March in non-leap years; this falls
     out of the tuple comparison.
  4. Reject a date of birth after today (server local date in the event timezone). Reject an age
     above 120.
  5. The age is never inferred from the UI locale. The check runs on the step save **and** in
     `require_complete_registration`, so a draft saved before the rule changed cannot bypass it.
* **Phone (S-10).**
  1. If the raw value starts with `+` or `00`, parse it without a region. Otherwise parse it with
     the selected region.
  2. Require `is_valid_number` and a type allowed by D-14.
  3. Store E.164 and `region_code_for_number`.
  4. Reject values of more than 20 raw characters after stripping separators.
* **URLs (S-11).** Use `urllib.parse` after scheme defaulting. Validate the host through IDNA,
  with no userinfo; the port is allowed only when it is 80 or 443. Reuse Django `URLValidator`
  with schemes `http` and `https`.
* **Choice fields.** Every select or multi-select value is validated against the active,
  selectable queryset on the server. A hidden or excluded code posted by hand is rejected with the
  generic "Select a valid choice."
* **Channel (D-12).** The effective public state is evaluated **inside** the draft-creation and
  final-submit transactions, under a row lock on `EventEdition`, so closure races fail closed.

## 8. Proposed Finder-based UX states

These are all adaptations inside project templates and CSS. Finder is a visual reference only.
Every reused element is recorded in `docs/design/finder_inventory.md` during implementation.

| Component | Desktop | Mobile | Arabic RTL | States |
| --- | --- | --- | --- | --- |
| OTP cells | 6 cells about 48 px wide, Finder input radius and focus ring | same, full width | the cell row stays LTR; the label and help text are RTL | empty, focused cell, filled, pasted, error (whole group `aria-invalid`), resend cooldown |
| Searchable select | a Finder `form-select` look that opens a listbox under the field, with a search box, highlighted match and localized "No results" | full-width sheet-like listbox, at least 44 px targets | caret on the left, text right-aligned, LTR codes isolated | closed, open, filtering, no result, selected, disabled, error |
| Phone group | `[Country (+code) ▾][ number ]` input group | stacked selector over the number | the group keeps LTR order so the prefix precedes the number | empty, defaulted, auto-switched after a `+` paste (announced), invalid |
| Date parts | three narrow inputs labelled Day, Month, Year with one group legend | same, `inputmode=numeric` | fields ordered right to left: Day, Month, Year | per-part error, whole-date error |
| Multi-select chips | chips wrap (standing rule), each with a remove button named "Remove {topic}" | chips wrap, no horizontal scroll | chips flow right to left | none, some, maximum reached |
| Counter | right-aligned "123 / 500" under the textarea | same | left-aligned, digits LTR | normal, 80% warning, limit |
| Footer | two-column: official links and contact/social | stacked | mirrored | links only; contact block hidden until verified |

## 9. Implementation grouping

| Package | Remarks | Content | Migrations | New dependencies | Entry condition |
| --- | --- | --- | --- | --- | --- |
| **UX-1** Brand, shared controls, metadata and footer | M02, M04, M05, M06 (existing fields), M09 (component), M10 (widget), M23 | S-01, S-02 (OTP part), S-04, S-05, S-06, S-07, S-08 (widget and parse), S-13; favicon per D-04; Arabic copy per D-03; robots and header middleware | **None expected** | None | UX-PLAN accepted; D-03 and D-04 answered (C-07 optional) |
| **UX-2** Identity, contact and professional data | M03, M07, M08, M09 (adoption), M10 (rules), M11–M20 | L-NAME and L-TEXT, NIN, age, catalogs, defaults, phone, URLs, counters, scope; all channels (open, invitation, delegation import, on-behalf claim, profile edits) | Additive (§12: rows 1–6) | None | D-01, D-02, D-05–D-09, D-14 and C-01, C-02, C-04–C-06 approved; UX-1 ready |
| **UX-3** Accommodation, privacy and terms | M21, M22 | accommodation model, encryption, permission, redaction; consent purposes; complete EN/FR/AR drafts; legacy free-text handling | Additive (§12: rows 7–9) | None | D-10, D-11, C-08 approved; C-09 facts may stay as release blockers |
| **UX-4** Channel control and CAPTCHA | M01, M24 | channel policy and matrix (§10), enforcement, audit, CAPTCHA evaluation then integration, CSP | Additive (§12: rows 10–11) | One CAPTCHA library **after** the recorded selection | D-12, D-13 approved |

Each package follows the playbook common contract. That means focused tests first, then
`scripts/check.py all`, then review and update archives, then a stop at `READY_FOR_LOCAL_REVIEW`.

## 10. Proposed channel matrix (M24, for D-12)

| Action | OPEN within window | INVITATION_ONLY or outside window | CLOSED |
| --- | --- | --- | --- |
| Landing page / OTP request / OTP verify | allowed | allowed (generic responses unchanged) | allowed (existing accounts need sign-in) |
| Start open draft (no invitation) | allowed | **blocked**: "Public registration is closed" page, HTTP 403 | blocked |
| Resume or submit an existing OPEN-source draft | allowed | **blocked** on save and submit; data kept | blocked |
| Campaign invitation link → invited draft → submit | allowed (existing checks) | allowed (validity, capacity, expiry and revocation rechecked at submit) | blocked |
| Invalid invitation with `fallback_mode=OFFER_OPEN_REGISTRATION` | fallback offered | fallback **suppressed**; generic unavailable page | suppressed |
| Delegation or on-behalf claim → complete → submit | allowed | allowed (claim token rules unchanged) | blocked |
| Staff on-behalf draft creation | allowed (permission) | allowed (permission) | blocked (D-12 alternative may allow it) |
| Sign-in, workspace, passes, confirmation pages | allowed | allowed | allowed |
| Operations: review, decisions, badges, entry, exports | allowed (permissions) | allowed | allowed |
| Change channel mode or window | permission `events.manage_registration_channels` (new, scoped), reason required, audit event, idempotent | same | same |

## 11. Security and privacy implications

* **Enumeration.** Closure must not reveal account existence. OTP responses stay generic in
  every mode, and the "closed" page appears only after verification.
* **Channel escalation.** A public draft must never become invitation-authorized through a
  changed field, query parameter or session value. Source kind and invitation link are
  server-held, as today (F-13), and are rechecked at submit.
* **Races.** A close-versus-submit race must resolve under a transaction-level lock (§7 Channel).
  Idempotent resubmission of an already submitted registration still returns its confirmation.
* **CAPTCHA.** Secrets live in the environment only and are never logged. Consumed challenges are
  kept in a replay store with expiry. Verification is bound to the action. Outage behaviour fails
  closed with an audited recovery switch. The CSP additions (script, worker) are the minimum
  needed. Low-end devices need a bounded proof-of-work cost.
* **Sensitive data.**
  * Accommodation data is health-adjacent sensitive data. It needs explicit consent (D-11),
    encryption at rest for the note, a dedicated permission, and exclusion from exports by
    default.
  * Redaction covers logs, audit payloads, snapshots and telemetry.
  * Today the free text sits in plaintext in the snapshot JSON and in `sessionStorage` (F-11).
    UX-3 must decide how new snapshots store it, for example category codes only with the note
    referenced but not copied. It must also remove it from language-switch preservation.
    Historical snapshots are immutable evidence and stay unchanged.
* **Identity.** The NIN and passport keep encryption and the versioned blind index. The
  normalization output equals today's canonical input, so no key or index change is needed.
* **Phone.** Storing the parsed region fixes a data-quality gap (F-7). Masking is unchanged.
* **URLs.** They are never fetched server-side, so there is no SSRF surface. Rendering is escaped
  with safe `rel` values.
* **SEO.** noindex, no-store and robots rules reduce leakage of token, claim and pass URLs. The
  existing `SECURE_REFERRER_POLICY=same-origin` stays.
* **Country policy.** An excluded code posted by hand is rejected server-side. Historical rows
  referencing a deactivated country stay valid (`PROTECT`).
* **Minimization.** No new identity attribute is collected. Accommodation stays voluntary and
  declining it carries no disadvantage.
* **Legal.** The gate produces no legal approval. Institutional facts missing from C-09 remain
  explicit release blockers.

## 12. Migration risk assessment

| # | Package | Change | Type | Risk | Mitigation |
| --- | --- | --- | --- | --- | --- |
| 1 | U2 | `Country`: add `catalog_version`, `is_selectable` (or reuse `is_active`) and, if D-05 changes, nationality labels (EN/FR/AR) | additive columns with defaults | Low | Nullable or default columns; no rewrite of existing rows |
| 2 | U2 | Country catalog import (about 250 rows) | data migration or idempotent command | Medium: FK `PROTECT` on profiles, identifiers and organizations | `get_or_create` style; never delete; deactivate excluded codes; test against rows referencing FR and DZ |
| 3 | U2 | `OrganizationType` extended choices | state-only (choices), codes ≤ 16 so no `ALTER` | Low | `sqlmigrate` must show no SQL, or only a check-free change |
| 4 | U2 | `Sector` and `InterestTopic` rows; `InterestTopic.group_code` | additive column plus data | Low | keep existing codes; reversible data migration |
| 5 | U2 | `ProfessionalAffiliation.operating_scope` | additive enum column, nullable for legacy rows | Low | legacy rows stay NULL, shown as "Not provided"; required only for new input |
| 6 | U2 | Minimum-age config (`EventEdition.minimum_participant_age`, a small integer) | additive with default 18 per D-09 | Low | a missing event start date fails closed |
| 7 | U3 | Accommodation request model (one-to-one with `Registration`): status enum, category codes, encrypted note | new table | Medium: sensitive data | encrypted field (existing `EncryptedTextField`), permission-gated selectors, redaction tests |
| 8 | U3 | `ConsentPurpose` rows (processing consent, sensitive-data consent) and new draft `LegalDocumentVersion` rows | data | Low | versioned; old acceptance records untouched |
| 9 | U3 | Legacy `accessibility_needs_text` | **no destructive change** | Medium | keep the column; hide it from general views; handle it through retention later |
| 10 | U4 | `EventEdition.public_registration_mode` plus an audit reason | additive enum, default OPEN (behaviour-preserving) | Low | the default equals current behaviour; windows start being enforced, so check that seeded events have NULL windows (F-13) |
| 11 | U4 | CAPTCHA replay store (challenge digest, expiry) | new table with index | Low | TTL purge task idempotent; no personal data |

No migration rewrites history. Nothing is applied to the development database without separate
approval. The development `entry.0005` remains unapplied, and it is outside UX scope.
Nonconforming legacy values (names, biographies over 500 characters, missing phone numbers, or
required URLs now optional) are preserved and handled by S-14.

## 13. Accessibility and RTL implications

* WCAG 2.2 AA: visible labels, programmatic `aria-describedby` for helper text and counters,
  error summary plus inline errors (existing pattern), and no auto-submit on the OTP.
* The combobox follows the WAI-ARIA 1.2 combobox pattern: `aria-expanded`,
  `aria-activedescendant`, and Up, Down, Home, End, Enter, Escape and type-ahead keys. It is
  announced with a count of results. Targets are at least 24 px (WCAG 2.5.8) and 44 px on mobile.
* Standing bidi rules: dynamic values in `<bdi>` / `<bdi dir="ltr">`, never a whole component
  forced to LTR. Chips wrap. Error-summary labels and messages are isolated.
* Digits in OTP, NIN, phone and date parts are LTR within RTL. Latin-only inputs use `dir="auto"`
  so typed Latin text aligns correctly in the Arabic UI.
* Arabic copy uses Thmanyah Sans (Arabic only, self-hosted, per the licence decision). EN and FR
  keep the system stack.
* Placeholder contrast is at least 4.5:1, and placeholders are never the only instruction.
* The CAPTCHA must be usable by keyboard and screen reader without a visual puzzle, localized in
  AR/FR/EN, with an accessible failure and retry message.
* Language switching keeps the existing safe-field preservation for new non-sensitive fields.
  Accommodation fields are excluded (S-18).

## 14. Dependency and licence inventory

| Item | Status | Licence | Decision |
| --- | --- | --- | --- |
| `phonenumbers` | already approved | Apache-2.0 | reused for calling codes and examples |
| Choices.js 11.0.2 (Finder) | not used | MIT | alternative to S-07 only |
| Cleave.js 1.6.0 (Finder) | not used | Apache-2.0 | not proposed; masks are project-owned and minimal |
| flatpickr 4.6.13 (Finder) | not used | MIT | not proposed (S-08) |
| CAPTCHA library | none | per candidate | selected and recorded in UX-4 before it is added (D-13) |
| Unicode CLDR data (C-02) | data source | Unicode licence | pinned version recorded in the catalog file |

No dependency is added by UX-1 to UX-3 under the recommended choices.

## 15. Evidence of no application change

The before and after SHA-256 manifests of `apps/`, `config/`, `templates/`, `static/`, `locale/`,
`scripts/`, `tests/`, `docs/`, `manage.py`, `pyproject.toml`, `uv.lock`, the project instruction file and
`README.md` are recorded in
the UX-PLAN file manifest (historical evidence). The only differences are
this new file and the appended entry in the project status record (historical, kept outside the repository).

## 16. Workflow record

The temporary single-reviewer workflow of the playbook (§2) is recorded here as **proposed** for
developer progression. This gate is a plan prepared by the implementation team: it is not an independent review and it
grants itself no approval. Independent review stays PENDING. Production readiness stays
NOT_AUTHORIZED.

On 2026-09-29 the owner accepted D-15 (see §17): the workflow stays recorded as **proposed**.
The acceptance changes nothing about the review status. An implementation-side review is not an independent approval.

## 17. Owner decision record

Recorded on 2026-09-29 by the UX-PLAN-DECISIONS package. The "Decision (verbatim)" column copies
the owner's wording. "Recorded status" is ACCEPTED, or OPEN when the owner left the item open.
No approval is inferred for an OPEN item.

| ID | Decision (verbatim) | Recorded status | Date |
| --- | --- | --- | --- |
| S-01…S-18 | Accepted. | ACCEPTED (all 18, no objection) | 2026-09-29 |
| D-01 | Accept recommended (phone mandatory for all new registrations, all channels). | ACCEPTED | 2026-09-29 |
| D-02 | Accept recommended (Latin-only names; no Arabic-script name field in V1). | ACCEPTED | 2026-09-29 |
| D-03 | Accept recommended (full Arabic name; "ASC 2026" only in compact contexts). | ACCEPTED | 2026-09-29 |
| D-04 | Accept recommended (derive compact mark from logo; I approve the rendered result visually in UX-1). | ACCEPTED (visual approval of the rendered mark is pending, in UX-1) | 2026-09-29 |
| D-05 | Accept recommended (country names under the "Nationality" label). | ACCEPTED | 2026-09-29 |
| D-06 | Accept recommended (organization name in Latin script). | ACCEPTED | 2026-09-29 |
| D-07 | Accept recommended (headquarters country + operating scope, no country list). | ACCEPTED | 2026-09-29 |
| D-08 | Accept recommended (biography optional, 500 max; department optional; job title 120). | ACCEPTED | 2026-09-29 |
| D-09 | Accept recommended (18 years on the event's first local day; no invitation exception). | ACCEPTED | 2026-09-29 |
| D-10 | Accept recommended (voluntary structured categories + optional encrypted note up to 300 characters). | ACCEPTED | 2026-09-29 |
| D-11 | Accept recommended (three separate statements + separate sensitive-data consent). | ACCEPTED | 2026-09-29 |
| D-12 | Accept recommended (OPEN / INVITATION_ONLY / CLOSED; public drafts cannot be submitted after closure). | ACCEPTED | 2026-09-29 |
| D-13 | Accept recommended (evaluate candidates in UX-4; ALTCHA leading candidate; OTP request placement). | ACCEPTED (no product is selected yet; UX-4 records the choice before any dependency is added) | 2026-09-29 |
| D-14 | Accept recommended (MOBILE or FIXED_LINE_OR_MOBILE). | ACCEPTED | 2026-09-29 |
| D-15 | Accept recommended (temporary single-reviewer workflow recorded as proposed). | ACCEPTED (workflow stays "proposed"; not an independent approval) | 2026-09-29 |
| C-01 | OPEN (Algerian recognition list not provided yet; exclusion of Israel and Kosovo confirmed). | **OPEN** (the exclusion of IL and XK is confirmed; every other geopolitical entry is undecided) | 2026-09-29 |
| C-02 | Accept recommended (pinned CLDR base, reviewed translations). | ACCEPTED | 2026-09-29 |
| C-04 | Accept the proposed organization-type list in §6.6. | ACCEPTED | 2026-09-29 |
| C-05 | Accept the proposed sector list in §6.6. | ACCEPTED | 2026-09-29 |
| C-06 | Accept the proposed interest list in §6.6. | ACCEPTED | 2026-09-29 |
| C-07 | OPEN (contact and social links to be verified from the official sites in UX-1). | **OPEN** (verification is planned in UX-1; no contact or social item is approved) | 2026-09-29 |
| C-08 | Accept the categories; sign-language naming OPEN. | ACCEPTED (categories) / **OPEN** (sign-language naming) | 2026-09-29 |
| C-09 | OPEN (institutional legal facts remain release blockers). | **OPEN** (release blocker, not a UX-3 start blocker) | 2026-09-29 |

C-03 and C-10 were not part of the owner's list. C-03 is resolved in §5.3 (no owner action).
C-10 is evaluated inside UX-4.

### 17.1 Open items

| ID | What stays open | Effect |
| --- | --- | --- |
| C-01 | The Algerian recognition list, the Western Sahara (EH), Palestine (PS), Taiwan (TW) and dependent-territory decisions, and the stateless / not-listed assistance path. Israel and Kosovo are excluded (confirmed; XK is simply not added). | Blocks the country catalog import and the selectable-country set (M07, §12 rows 1 and 2). Also the calling-code selector's country set (S-10), which is built from that catalog. |
| C-07 | Footer contact and social items, and the Ministry's official names in AR, FR and EN. | Does not block UX-1 (optional per §9). The footer ships only the two S-13 links. Contact and social items appear only if verified from the official sites in UX-1; unverified items are omitted, never guessed. |
| C-08 (naming) | Algerian Sign Language versus International Sign wording for `SIGN_LANGUAGE`. | UX-3 uses a neutral label ("Sign language interpretation") that names no specific sign language, and records the open naming question. |
| C-09 | Controller legal identity and address, data-protection contact, ANPDP reference, purposes, recipients, hosting and transfers, retention periods, rights route. | Release blockers. UX-3 drafts everything possible and keeps the missing facts as explicit configuration blockers. |
| D-04 (visual approval) | The owner's visual approval of the rendered compact favicon mark. | Checkpoint inside UX-1 review. It is not a start blocker. |
| D-13 (product) | The CAPTCHA product is not chosen. | UX-4 evaluates the candidates and records the choice before any dependency is added. |

### 17.2 Gate sections changed by these decisions

The owner chose the recommended option for every answered decision item, and no alternative or
edit. Therefore the field contract (§6), validation rules (§7), migration table (§12), channel
matrix (§10), decisions tables (§5), amendment ledger (§4) and implementation grouping (§9)
are **unchanged**. The complete list of changes made by UX-PLAN-DECISIONS to this document is:

1. The header: status, work package, previous status and decisions date.
2. §0: one bullet recording that decisions were recorded.
3. §16: one paragraph recording the D-15 acceptance.
4. §17: the decision table above, §17.1, §17.2 and §17.3.
5. §18: the consumed prompt is replaced by the next-step note.

The recommended options that the unchanged sections already describe are now the accepted
policy for: D-01 and A-01 (phone mandatory), D-02 and A-02 (Latin-only names, FR-PER-002
deferred), D-06 and A-03 (Latin organization name), D-08 and A-04, D-09 and A-05, D-10 and A-06,
D-12 and A-07, D-03 and A-08, and D-13 and A-09.

### 17.3 Package readiness after the decisions

Entry conditions are those in §9. "Unblocked" means that the entry conditions are met. It does
not authorize a start: each package needs the developer's own authorization.

| Package | State | Entry condition check | Remaining blockers and limits |
| --- | --- | --- | --- |
| **UX-1** | **UNBLOCKED** | UX-PLAN accepted; S-01…S-18 accepted; D-03 and D-04 answered; C-07 optional. | No start blocker. Limits: D-04 needs the owner's visual approval of the rendered mark during UX-1 review; C-07 items are verified from the official sites in UX-1 and otherwise omitted. |
| **UX-2** | **BLOCKED for the M07 catalog; otherwise ready after UX-1** | D-01, D-02, D-05…D-09, D-14 and C-02, C-04…C-06 are approved. **C-01 is OPEN**, so the literal §9 entry condition is not met. UX-1 must first be ready for review. | C-01 blocks the country catalog import, the excluded-entry list beyond IL and XK, and the calling-code selector's final country set. Independent work (M03, M08, M09 adoption, M10 rules, M11 to M20, M12, M13 logic) can proceed if the developer chooses to start UX-2 with the catalog carved out. That work must not invent a recognition policy, and the catalog work stays a documented blocked feature. |
| **UX-3** | **UNBLOCKED** | D-10, D-11 and C-08 categories are approved. C-09 may stay a release blocker per §9. | Sign-language naming is OPEN (neutral label meanwhile). C-09 legal facts remain **release blockers**. No legal or ANPDP approval is claimed. |
| **UX-4** | **UNBLOCKED** | D-12 and D-13 are approved. | The CAPTCHA product is not selected. UX-4 records the selection from the C-10 evaluation before it adds a dependency. |

Phase 4 Prompt 4 is separate from UX and was not started. No package was started by this
recording.

### 17.4 Later owner statements and delegated decisions (2026-09-29, during UX-4 to UX-2)

| Item | Owner statement (chat, 2026-09-29) | Recorded outcome |
| --- | --- | --- |
| Package order | "Start all packages in the order you consider logical." | UX-4, then UX-3, then UX-2 were run. |
| D-04 visual approval | "The icon is very good." | The rendered favicon mark is **approved**. |
| E-mail naming | "Agreed on the e-mail and the Arabic name … you have freedom of decision." | Delegated to the implementation team and decided in UX-2: localized `EventEdition` names, plus Arabic `v2-draft` e-mail templates that name the event in full (the v1 versions are retired, not rewritten). |
| C-07 | Same statement (delegated). | Decided in UX-2. Contact and social items were **verified on the official conference site** in a browser on 2026-09-29 and added to the footer. The ministry site is "under development", so its postal address is not used in legal text. No item was guessed. |

C-01, C-09 and the sign-language naming (C-08) remain OPEN.

### 17.5 UX-C2 authorization and owner decisions (2026-09-30)

In the chat that authorized UX-C2, the developer stated that they explicitly approved all four
decisions and all four correction items from the independent UX-C1 evidence review. The earlier
rows above are unchanged.

| Item | Owner statement (UX-C2 authorization, 2026-09-30) | Recorded outcome |
| --- | --- | --- |
| UX-F04 | "UX-F04: approved R1, guarded fail-safe reversal". Editing the four unapplied migration files is authorized; no new provenance schema; no development migrate. | ACCEPTED: option R1 of the historical review record `phase_04_ux_c1_decision_note.md` (kept outside the repository) §1.3, with the stricter UX-C2 rules (no ownership inferred from content, labels, hashes or timestamps alone; atomic refusal). |
| UX-D01 | "UX-D01: approved Option M, self-hosted ALTCHA". The maintained widget and the official Python library replace the custom solver; session binding; no CDN, ALTCHA Cloud or remote verification; the recovery bypass stays off. | ACCEPTED: option M. |
| Accommodation | "Hide submitted YES-without-details requests from support unless that registration's own submission records explicit sensitive-support consent"; a voluntary consent for such a request is recorded with a registration-scoped consent fact; UX-C1 consented submissions stay valid through a documented compatibility rule; withdrawal stays registration-scoped; legacy-only text stays hidden. | ACCEPTED. |
| Correction items | Strict sensitive consent (A), authoritative phone validation at submission (B), Registration-first locking of the remaining wizard writes (C), investigation of the two observed aggregate failures (D). | Authorized for implementation in UX-C2. |
| Documentation | The superseded phone rule in the project engineering rules is updated to "phone required for all new registrations", citing D-01 and A-01. | Done in UX-C2. |

C-01 and C-09 remain OPEN. No country-recognition or legal fact is added by this record.

### 17.6 UXR-C1 authorization and owner decisions (2026-09-30)

The UX-R review (the historical review record `phase_04_ux_r_report.md` , kept outside the repository) returned CHANGES_REQUIRED. In the chat
that authorized UXR-C1, the developer explicitly authorized the following. The earlier rows above
are unchanged.

| Item | Owner statement (UXR-C1 authorization, 2026-09-30) | Recorded outcome |
| --- | --- | --- |
| UXR-F01 to UXR-F06 | Corrections UXR-F01 through UXR-F06 are authorized. | Implemented in UXR-C1 (the historical review record `phase_04_uxr_c1_report.md` , kept outside the repository). |
| Arabic Latin-letter wording | "استخدم الحروف اللاتينية فقط؛ يُسمح بالحروف ذات العلامات مثل é." | ACCEPTED. It replaces the ambiguous "(يُسمح بعلامات التشكيل)" in the name and professional-text messages. English msgids and the validation rule are unchanged. |
| Support visibility | Hide accommodation requests from support after their registration is withdrawn or cancelled, while preserving existing retention and legal-hold behaviour. | ACCEPTED. Only the support selector changes; no row is deleted or rewritten. |

C-01, C-09 and the C-08 sign-language naming remain OPEN. No legal content, consent requirement,
country-recognition fact or catalog is added by this record.

### 17.7 P4-4 owner decision FOOTER-01 (2026-10-01)

After the UXR-C1 archive was built, the developer reduced `templates/partials/footer_official.html`
to its header comment, because they were not satisfied with the footer UI (confirmed during P4-4 on
2026-09-30). In the P4-4 session on 2026-10-01 the developer chose option (a) of FOOTER-01: record a
formal amendment and update the affected footer tests to the new design. The earlier rows above are
unchanged.

| Amendment | Supersedes or affects | Recorded outcome |
| --- | --- | --- |
| A-10 | M23 and S-13 (the two official-site links in the shared footer), the C-07 footer contact and social block (UX-2), and UXR-F02's footer link to the legal page | ACCEPTED. The shared footer renders no link, contact item or social item until the developer supplies a new footer design. The start page keeps its own legal link (UXR-F02), and the legal page stays reachable at `/legal/`. Only the footer tests that pinned the removed content change, and they now assert the empty footer. Nothing else is weakened: the no-third-party-resource guard stays, applied to the whole footer. |

A new footer design, when supplied, is a separate change with its own tests. C-07 values stay
unverified; this record adds no contact, social or legal fact.

### 17.8 P4-4-C1 owner statements (2026-10-01)

The independent P4-4 review could confirm that A-10 was recorded and implemented consistently, but
not authenticate the owner's selected answer, and asked for one confirmation. In the chat that
authorized P4-4-C1, the developer stated the following. The earlier rows above are unchanged.

| Item | Owner statement (P4-4-C1 authorization, 2026-10-01) | Recorded outcome |
| --- | --- | --- |
| FOOTER-01 / A-10 | "FOOTER-01 / A-10 is explicitly confirmed: keep the shared official-footer partial empty. Do not restore the baseline footer." | CONFIRMED. A-10 (§17.7) stands as recorded; no footer file or test changes in P4-4-C1. |
| MFA-01 (fact) | "Institutional SSO with MFA is UNKNOWN. Do not assume it exists or is unavailable." | RECORDED AS UNKNOWN. Mandatory operational MFA is already required (TRD §16.1); only the provider family is open. Options: `docs/security/p4_4_c1_mfa_options_comparison.md`. |

No MFA dependency, provider integration, shared-cache policy or P4-5 work is approved by this
record.

### 17.9 P4-4-C3 owner decisions MFA-01 and CACHE-01 (2026-10-01)

In the message that authorized P4-4-C3, the developer, as owner, recorded the two decisions below.
They supersede the open MFA-01 and CACHE-01 rows of the P4-4-C1 owner brief. The earlier rows above
are unchanged, including the §17.8 record that institutional SSO availability is UNKNOWN; that fact
stays recorded but no longer gates sign-in.

| Amendment or decision | Supersedes or affects | Recorded outcome |
| --- | --- | --- |
| A-11 (MFA-01, revised operational sign-in requirement) | The sign-in MFA requirement of TRD §16.1 ("ASC staff: … mandatory MFA"; "Organization delegate: … MFA when elevated"; "External security user: named, time-bounded account with MFA"), PRD FR-AUTH-009 ("shall enforce MFA for privileged operational accounts") and AF-AUTH-03 ("MFA is mandatory for operational users"), for every `OperationalUser` on the operational sign-in path: administrators, temporary operators and external security accounts. It also supersedes the earlier staging-only exception. | ACCEPTED. Operational users sign in with email and password in **both staging and production**. This is a change to the requirement, not a temporary MFA deferral, and MFA is not reported as implemented (`OPERATIONAL_SIGN_IN_MFA_ENFORCED` stays False). Unchanged: participant email OTP; password attempt limits; permissions and scopes; account status and expiry; session lifetimes; sign-out; audit records. **Not authorized:** any bypass of the step-up protection of sensitive operations. The emergency device wipe still requires an MFA step-up and fails closed without a genuine provider. The read-only specifications stay unedited; this record is the reference. |
| CACHE-01 (approved counter direction) | Brief §C options C1 + D3; P44-F09 / R-05 | ACCEPTED as a direction. ALTCHA challenge issuance uses a shared Redis counter in staging and production, with a stricter, bounded per-process fallback when Redis is unavailable, and sanitized, rate-limited degradation and recovery alerts. The exact limits are engineering defaults to document and verify (ADR-0025), not owner-approved event-capacity figures. |

The step-up provider for sensitive operations is not decided by either record. Without a genuine
provider, the emergency device wipe is unavailable; `manage.py release_readiness` reports this, and
the question is open as STEPUP-01 in the P4-4-C1 owner brief (§D, added by P4-4-C3).

### 17.10 Identity verification decisions and their status (PLAN-SYNC, 2026-10-01)

The owner recorded these decisions in the identity addendum (v2.0, 2026-10-01). PLAN-SYNC
integrated it as `docs/execution/identity_verification_plan_addendum.md` (v2.1), after P4-4-C4
passed its bounded independent review.

**Records.**

* The decisions that change specification text form amendment **A-12**. Its exact section
  references are in `docs/execution/requirements_overlay_2026-10-01_identity_and_auth.md`.
* The earlier rows above are unchanged.
* The AUTH and CACHE decisions restate §17.9; they are not new amendments.
* "Recorded status" follows the rule of §17: ACCEPTED, or OPEN where a detail is still undecided.
  No implementation is implied.

| ID | Decision (short) | Recorded status | Undecided detail |
| --- | --- | --- | --- |
| AUTH-01 | Operational staff sign in with email and password in staging AND production | ACCEPTED (= A-11, §17.9). Implemented in P4-4-C3 (no code change; MFA not enforced) | — |
| AUTH-02 | Participants keep email OTP | ACCEPTED. Unchanged | Real mail delivery evidence (UAT-02) |
| AUTH-03 | Permissions, scopes, throttling, expiry, sessions and auditing preserved | ACCEPTED. Unchanged | — |
| AUTH-04 | Emergency-wipe step-up stays fail-closed; no fake MFA provider | ACCEPTED. Unchanged | STEPUP-01 (genuine provider or accepted unavailability) |
| CACHE-01 | Shared Redis counter, stricter bounded fallback, alerts | ACCEPTED (§17.9). Implemented in P4-4-C3 and corrected in P4-4-C4, which passed the bounded independent review | Real Redis NOT_PROVEN; CACHE-02; REDIS-01 |
| IDV-01 | Algerian NIN registration uses the ministry API | ACCEPTED (A-12). Not implemented | API-01, API-04, API-05; SCOPE-01 |
| IDV-02 | The returned NIN must belong to the submitted identity; existence alone is insufficient | ACCEPTED (A-12) | Exact comparison rules in IDV-1 |
| IDV-03 | Minor name differences may be corrected from official data, with provenance | ACCEPTED (A-12) | **OPEN:** MATCH-01 (safe correction policy); MIN-01 (retained official facts, in relation to D-02) |
| IDV-04 | Major mismatch, NIN not found, ambiguous data and exhausted failures go to manual review with a reason | ACCEPTED (A-12) | — |
| IDV-05 | Staff NIN correction with evidence, a reason, duplicate checks and a new API attempt | ACCEPTED (A-12) | REVIEW-01 (permission mapping) |
| IDV-06 | Manual verification from the national identity card, distinguishable from API verification, never overriding cross-person duplicates | ACCEPTED (A-12) | REVIEW-01 (required reasons) |
| IDV-07 | Duplicates rechecked after corrections and at final verification, under concurrency, including pending records | ACCEPTED (A-12) | Locking design in IDV-1 |
| IDV-08 | Every foreign participant: manual passport verification, identity page required, no ministry calls | ACCEPTED (A-12) | **OPEN:** SCOPE-01 (an Algerian national on the passport path) |
| IDV-09 | Correction and resubmission on the same account and Registration; final rejection is separate | ACCEPTED (A-12) | — |
| IDV-10 | Identity verification is separate from participation and accreditation acceptance | ACCEPTED (A-12) | — |
| IDV-11 | Explicit date normalization; ambiguous dates are never guessed | ACCEPTED (A-12) | **OPEN:** API-02, API-03 (formats; presumed dates) |
| DOC-01 | Identity card evidence for Algerian manual review; passport identity page required | ACCEPTED (A-12) | **OPEN:** DOC-02 (formats, sizes, masking, retention mapping); OD-007 |
| UAT-01 | Realistic manual tests for every role and account, including real OTP email | ACCEPTED (planned acceptance material) | UAT-02 (environment and method) |
| UI-01 | Keep the official footer empty | ACCEPTED (= A-10, §17.7, §17.8) | FOOTER-02, if a new design is wanted |

**Sequencing.** The feature packages IDV-1 to IDV-4 come before P4-6. Whether they also come before
P4-5 is **OPEN** (SEQ-01). STUB-01 (refusing the NIN stub in staging and production) is a new
question that PLAN-SYNC raised; it is not an owner decision.

No identity implementation, P4-5 or deployment work is approved by this record.

### 17.11 Identity implementation decisions after PLAN-SYNC (2026-10-01)

The developer, as owner, supplied the identity implementation handoff of 2026-10-01
on 2026-10-01 and instructed the implementation team to implement IDV-1 to IDV-4 in one assignment and deliver one
final review package, without deploying. The handoff records decisions made after PLAN-SYNC. They
form amendment **A-13**; the exact section references are in
`docs/execution/requirements_overlay_2026-10-01_A13_identity_implementation.md`. The §17.10 rows
above are unchanged; the table below records how each open detail was settled.

| Open item in §17.10 or the register | Owner decision (short) | Recorded status |
| --- | --- | --- |
| SCOPE-01 | Algerian nationals must use the NIN route, with an explicit staff-assisted exception; no self-service passport bypass | ACCEPTED (A13-01) |
| IDV-08 detail | Every foreign national: manual passport review, no ministry lookup | ACCEPTED (A13-02) |
| MIN-01 | Minimum official Latin names and birth-date facts only; no Arabic names, parental or civil-status data, no full bodies; presumed dates keep only the flag and outcome | ACCEPTED (A13-03) |
| MATCH-01 | Automatic correction limited to case and whitespace; everything else manual; no fuzzy threshold | ACCEPTED (A13-04) |
| DOC-02, REVIEW-01 | Reuse existing evidence formats, limits, storage and scanning; map dedicated permissions onto existing scoped roles with a documented mapping | ACCEPTED (A13-05). Legal retention periods stay OPEN (OD-007) |
| STUB-01 | Refuse every stub or mock official backend in staging and production; distinguish simulations | ACCEPTED (A13-06) |
| SEQ-01 | IDV-1 to IDV-4 in one assignment, before P4-5 | ACCEPTED (A13-07) |
| API-03 | `presume` true: ignore the ministry date entirely, keep the entered date, verify on NIN and names; record `skipped_presumed` | ACCEPTED (A13-08, A13-09) |
| API-05 | The malformed sample was a copy artifact; malformed live responses are INVALID_RESPONSE | ACCEPTED (A13-10) |
| API-04 | No usage limits (owner report); access, availability and error contract unknown | QUALIFIED (A13-11); the remainder stays OPEN |
| API-01 | Authentication response contract not supplied; configurable validated mapping | **OPEN** (A13-12 is the design, not the input) |
| API-02 | Non-presumed official date formats beyond DD/MM/YYYY | **OPEN** (A13-13 is the rule) |
| Authentication, counter, footer | Unchanged (A-11, CACHE-01, A-10) | CONFIRMED (A13-15) |

This record authorizes the IDV-1 to IDV-4 implementation only. It does not authorize P4-5,
deployment, live ministry or mail calls, or any change to the read-only specifications.

### 17.12 Registration correction package: owner decisions (2026-10-04)

On 2026-10-04 the owner reported six registration defects after the staging Beta (correction 1)
and authorized one consolidated correction package, without deployment. The decisions it
records, and their implementation:

| Item | Owner decision (short) | Recorded status |
| --- | --- | --- |
| C-01 country catalog | The supplied catalog of 248 countries and territories (ISO 3166-1 codes, CLDR names in EN/FR/AR, territories included) is approved; Palestine is `PS` with the supplied labels; `IL` and `XK` are excluded. Install and reconcile it on fresh and existing databases; never delete a referenced row | ACCEPTED (catalog `countries-v1`, `core.0005`, ADR-0028). The "stateless / not listed" assistance path stays **OPEN** |
| FOOTER-02 | Show exactly two official lines, "Ministère de l'Économie de la Connaissance, des Start-up et des Micro-entreprise" and "Direction des Systèmes d’Information (DSI)" (smaller), in every language, with no prefix | ACCEPTED. Supersedes only A-10's empty footer partial (§17.7); the removed links, contact and social items stay removed |
| Legal notices | Replace the draft presentation of the Privacy Notice and Registration Terms with official published versions, through the existing publication mechanism, without editing accepted versions or inventing facts | ACCEPTED as version `v3` (`privacy.0005`). The C-09 facts the owner has not supplied keep their markers and remain production blockers |
| Profile photograph | Required for every new submission; never shown as optional | ACCEPTED |
| Passport copy | Required before submission on the passport route, whatever the residence; the route follows the nationality | ACCEPTED (confirms A13-02 and adds the missing FR/AR wording) |
| Register again after withdrawal | A participant who withdrew can create a new, separate registration from the same account, by the origin-aware rules of IDV-Q19; the withdrawn registration is never reopened | ACCEPTED |

Implementation choices made within these decisions (reviewable, not owner decisions): a registration
the registration team cancelled offers no self-service re-registration (the participant is told to
contact the team); a withdrawn registration that was replaced by a new one cannot be reopened by
staff; a withdrawn invitation submission still counts toward its campaign's capacity (unchanged
rule). This record authorizes the correction package only, not deployment.

## 18. Next step

The prompt that was proposed here for recording the decisions has been executed as
UX-PLAN-DECISIONS. Status: **UX_PLAN_DECISIONS_RECORDED**.

Recommended order, subject to the developer's choice and authorization:

1. **UX-1**, in a fresh session, with the playbook STARTER and `WORK_PACKAGE_ID = UX-1`. It
   has no start blocker.
2. **UX-3** or **UX-4**, either of which has no start blocker and no dependency on UX-1 or UX-2.
3. **UX-2**, after UX-1 is ready for review and after the developer either provides C-01 or
   authorizes UX-2 with the M07 catalog carved out.

The developer can supply C-01 (an official list, or line-by-line approval of the proposed list),
C-07 (verified contact and social values), the sign-language naming and the C-09 legal facts at
any time. Each one only unblocks its own feature.
