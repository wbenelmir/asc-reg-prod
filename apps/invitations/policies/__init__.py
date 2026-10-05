"""Authorization for apps.invitations.

No project-owned predicate is needed here yet: every check reuses
`apps.accounts.policies.has_scoped_permission` directly, and every
scoped-visibility selector lives in `apps.invitations.selectors`
(Phase 2 Prompt 2).
"""
