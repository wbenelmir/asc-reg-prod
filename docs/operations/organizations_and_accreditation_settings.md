# Organizations and accreditation settings

## 1. Organizations (`/ops/organizations/`)

Code: `apps/organizations/views.py`, `apps/organizations/services/__init__.py`,
`apps/invitations/views.py` (campaigns, links, delegation lists).

Permissions: `organizations.view_organization`, `add_organization` and
`change_organization`. No existing staff role holds them and this update
creates no role, so organization management belongs to the superadmin. Other
staff keep their scoped workspace actions (campaigns, delegation lists,
exports) under their existing roles. Every lookup is scope-filtered (an
out-of-scope id is a 404) and every change is audited. If organization access
is delegated to a staff role, the organization pages show only what that
member may see in each subordinate scope: registration counts through the
registration view scope, campaigns and delegation lists through their own
view scopes (event and organization). An organization taking part in
several events therefore shows another event's campaigns and counts only to
readers scoped to that event, and to the superadmin.

From the list (search, status filter, counts, pagination) an authorized user
can create, edit, activate and deactivate an organization. From an
organization:

* **Campaigns**: create, open, close, edit (name, validity dates,
  registration limit, default language), copy the registration link, see
  each link's status, disable a link and issue a replacement. A campaign is
  edited with a version check: two people editing at once get a conflict,
  never a silent overwrite. A closed campaign is history and is not edited.
  A limit lower than the places already used is accepted: it only stops new
  submissions.
* **Delegation lists**: upload, preview and apply (unchanged workflow).
* **View registrations**: the registration list filtered on the organization,
  the campaign or the link.
* **Export**: the export form pre-filled with the event, organization and
  campaign.

An inactive organization keeps its history, but new campaigns, delegation
uploads and on-behalf registrations are refused for it.

Capacity: a campaign limit is enforced under a lock on the campaign at
submission time, so simultaneous submissions never exceed it (covered by
`tests/concurrency/test_invitation_campaign_capacity.py`). A withdrawn
registration keeps its counted place; raising the limit is the way to admit
more.

## 2. Accreditation settings (`/ops/accreditation/settings/`)

Code: `apps/accreditation/settings_admin.py`, `apps/accreditation/settings_views.py`.

Navigation: **Accreditation settings**. Per event edition: participant roles,
badge types, access profiles and access rules, with their current assignment
counts. Reading needs `accreditation.view_participantrole` in the event
(Accreditation Coordinators hold it); creating needs
`accreditation.add_<setting>` and editing or (de)activating
`accreditation.change_<setting>`, checked for the setting's own event. No
existing role holds the add/change permissions, so settings are managed by
the superadmin. Codes
are upper-case and unique per event; a setting in use is deactivated, never
deleted, and existing assignments are kept.

### Bulk accreditation

The bulk list contains final-approved registrations only (current decision
"Approved", not withdrawn or cancelled). It was empty before this update
because no role, badge type or access profile existed in the event: the page
now says which settings are missing and links to them.

The preview and the execution are separate checks: at execution every row is
checked again (still final-approved, still in the user's scope, setting
still active). Rows that no longer qualify are skipped and counted by reason
("no longer eligible", "not authorized", already assigned...). The result
page shows the counts and the audit event records them.
