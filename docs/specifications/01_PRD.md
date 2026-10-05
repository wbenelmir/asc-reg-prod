# ASC 2026 Registration Platform

## Product Requirements Document

**African Startup Conference 2026**

Web Registration, Accreditation and Entry Management Platform

> **Document Purpose**
>
> Define the complete product requirements for the ASC 2026 Registration Platform, including product scope, user journeys, functional and non-functional requirements, data and privacy controls, integrations, analytics, operational readiness, risks and release acceptance.

> **Central Product Rule**
>
> Every applicant completes one universal registration process. Applicants never select a participant role, badge category or access level. These attributes are assigned only by authorized platform users after review.

| Attribute | Value |
| --- | --- |
| Document type | Product Requirements Document |
| Project | ASC 2026 Registration Platform |
| Coverage | Web registration, accreditation, credential management, physical-badge operations and entry verification |
| Language | English |
| Product languages | English, French and Arabic |
| Status | Final |
| Version | 1.1 |
| Date | September 2026 |

---

# Document Control

## Document Information

| **Attribute** | **Value** |
| --- | --- |
| Document title | ASC 2026 Registration Platform Product Requirements Document |
| Project name | ASC 2026 Registration Platform |
| Document identifier | ASC-REG-PRD |
| Version | 1.1 |
| Status | Final |
| Date | September 2026 |
| Document owner | ASC 2026 Registration Platform Project Team |
| Intended audience | Product, operations, security, design, engineering, testing and event-management teams |
| Language | English |
| Product languages | English, French and Arabic |
| Coverage | Complete product baseline, Sections 1 through 18 |

## Document Release

| **Version** | **Date** | **Status** | **Description** |
| --- | --- | --- | --- |
| 1.0 | September 2026 | Final | Approved product baseline for the ASC 2026 Registration Platform, covering Sections 1 through 18. |
| 1.1 | September 2026 | Final | Cross-document alignment of continuity, participant accounts, contact policy, status models, assignment boundaries, data classification, performance targets and legal governance. |

## Review and Approval

This PRD is the final product baseline for the ASC 2026 Registration Platform. Any subsequent product change shall be handled through controlled change management and shall not create an application-level approval workflow.

The responsible review and governance functions are:

- product ownership and project coordination;
- conference operations and registration management;
- event security and entry-control representation;
- technical architecture and engineering;
- data protection and legal review where required;
- quality assurance and operational readiness.

Formal sign-off records are maintained through the project governance process.

## Related Documents

| **Document** | **Relationship to this PRD** |
| --- | --- |
| ASC Registration Platform Project Scope and Discovery Summary | Approved pre-PRD baseline and source of the product principles consolidated in this document |
| ASC Registration Platform Application Flow Specification | Detailed applicant, administrative and security flows derived from the PRD |
| ASC Registration Platform UI/UX Design Specification | Information architecture, screens, interaction behavior, responsive rules and accessibility design |
| ASC Registration Platform Technical Requirements Document | Technical architecture, security controls, infrastructure and runtime requirements |
| ASC Registration Platform Backend Schema and Data Model Specification | Authoritative logical data model, constraints and implementation rules |
| ASC Registration Platform Implementation Plan | Delivery phases, test strategy, acceptance execution, deployment and operational handover |

> **Document Boundary**
>
> This PRD defines what the product must achieve and the rules it must enforce. It does not define database tables, detailed API payloads, frameworks, infrastructure implementation or source-code structure.

# Executive Summary

The ASC 2026 Registration Platform is a web-based product for registering, reviewing, accrediting and admitting participants to the fifth African Startup Conference. The event is scheduled for 5--7 December 2026 at the International Conference Center Abdelatif Rahal in Algiers under the theme *DEEPTECH: The Real Challenge*.

The platform will provide one neutral registration experience for all applicants in English, French and Arabic. It will collect identity, contact, professional and participation information without allowing applicants to choose or request a participant role, badge category or physical access level. Authorized platform users will review each submitted application and assign the appropriate internal participant role, badge type and access profile.

The product supports both open registration and controlled organization-level invitations. A ministry, public body, delegation, partner or other organization may receive a reusable invitation link. Registrations created through that link are associated automatically with the invitation campaign and the inviting organization, while accreditation remains an internal administrative decision.

The first operational release covers the complete path from registration through identity verification, administrative review, qualification, digital-entry-pass issuance, pre-event production and event-time distribution of generic physical badges, and multi-method entry verification. The product will include a public registration portal, an operations back office and a restricted security and entry portal using one shared backend and permission model.

> **V1 Outcome**
>
> ASC operations must be able to receive registrations, link them to organizations, review and qualify applicants, issue secure digital entry passes, manage generic physical-badge stock and distribution, verify entry and audit every sensitive action within one coherent platform.

# Background and Event Context

## ASC 2026 Context

The African Startup Conference is a continental event bringing together public institutions, ministerial delegations, investors, startups, speakers, media organizations, researchers, partners and professional visitors. These participants have different operational, accreditation and physical-access needs.

The broader ASC digital ecosystem also includes a separate mobile-application initiative and event-experience capabilities such as agenda access, matchmaking, Deal Rooms, maps and lead retrieval. This project is intentionally restricted to the web registration, accreditation, badge and entry-management domain.

## Current Registration Need

The conference requires a registration process that is simple for applicants and operationally useful for organizers. A public form must collect sufficient information for qualification without encouraging applicants to self-assign a prestigious category or badge.

The organizing team also requires a structured method to:

- receive registrations from the general public and invited organizations;
- associate registrations with ministries, bodies, delegations and partners;
- verify identity information with the minimum necessary documentation;
- identify duplicates and incomplete applications;
- request specific additional information;
- assign internal roles, Badge Types and access profiles;
- issue, revoke and replace secure Digital Entry Passes;
- manage pre-produced generic Physical Badges and record their distribution;
- verify entry while limiting the information visible to security users;
- preserve a complete history of sensitive actions and decisions.

## Problem Statement

Using multiple role-specific forms would cause applicants to choose or infer their own badge category, create inconsistent information, encourage incorrect self-classification and make future changes difficult. Collecting identity documents from every applicant by default would also increase privacy risk and operational burden without always improving verification.

The product must therefore combine a universal public journey with a controlled internal qualification process. It must collect facts from applicants, preserve institutional invitation context, reduce sensitive-document storage and give authorized users the tools required to make direct and traceable decisions.

## Product Opportunity

The platform creates an opportunity to establish one reusable participant record and one operational source of truth for registration, accreditation, credential readiness, physical-badge issuance and entry events. It can reduce duplicate registrations, shorten review time, simplify institutional invitation tracking and provide the separate mobile application with approved non-sensitive participant data through a controlled interface.

> **Avoided Product Pattern**
>
> The platform must not be designed as a collection of badge-specific registration forms. Participant categories are internal outputs of review, not public entry paths.

# Product Vision

## Vision Statement

> **Product Vision**
>
> Provide a secure, neutral and operationally efficient registration and accreditation platform that enables every ASC 2026 applicant to use one consistent journey while authorized teams retain full control over qualification, badge assignment and physical access.

## Product Objectives

The platform will:

- provide one responsive public registration journey for all applicants;
- support open registration and reusable organization-level invitation campaigns;
- preserve the distinction between inviting organization and participant organization;
- verify identity using NIN or passport information according to the participant context;
- avoid unnecessary collection and retention of identity documents;
- allow authorized users to review applications and execute permitted actions directly;
- manage internal participant roles, badge types and access profiles as separate attributes;
- support multiple Badge Type Assignments for one person when they represent authorized registration or accreditation contexts;
- generate secure Digital Entry Passes with opaque QR values or Registration References;
- manage quantities and issuance of generic Physical Badges produced by type before the event;
- provide a restricted interface for security verification and entry control;
- expose only approved, non-sensitive information to external applications;
- maintain complete auditability for sensitive actions.

## Success Outcomes

The product will be considered operationally successful when:

- an applicant can complete registration without selecting a badge or participant category;
- a registration created through an institutional link is associated correctly with its campaign and inviting organization;
- an authorized user can review, request information, decide and classify an application within one workflow;
- the platform can verify or route identity information for manual examination without unsafe automatic conclusions;
- approved participants can receive one or more contextual Badge Type Assignments and Digital Entry Passes that can be revoked or replaced independently;
- approved participants can receive the appropriate generic Physical Badge without making that badge the sole proof of identity;
- entry personnel can validate a participant by Digital Entry Pass, NIN, passport number, Registration Reference or authorized manual lookup using only the information necessary for entry control;
- sensitive identity information is absent from QR codes, general logs and the mobile integration interface;
- every sensitive operational action can be attributed to the user who performed it.

Quantitative success targets will be added after the expected registration volume, concurrency and review capacity are confirmed.

## Product Principles

| **Principle** | **Meaning** |
| --- | --- |
| Universal Neutral Registration | Every applicant completes the same general registration process and never selects a role, badge or access level. |
| Human Qualification | The system presents facts and verification results; authorized users make the final classification. |
| Direct Authorized Actions | A permitted action takes effect immediately without a secondary approval chain. |
| Data Minimization | The platform collects and stores only information necessary for registration, accreditation, security and entry. |
| Role and Group-Based Access | Users receive capabilities through one or more roles, directly or through groups. |
| Separation of Role Badge and Access | Participant role, badge type and physical access profile remain distinct configurable attributes. |
| Contextual Multiple Assignments | One person may hold multiple active Badge Type Assignments and Digital Entry Passes, while every assignment retains its own registration context, access profile, lifecycle and audit history. |
| Credential Separation | Badge Type Assignment, Digital Entry Pass, generic Physical Badge and Physical Badge Issuance Event are separate product concepts. |
| Auditable Sensitive Actions | Sensitive decisions and changes record actor, time, previous value, new value and reason where applicable. |

# Goals and Non-Goals

## Product Goals

V1 has the following product goals:

1. deliver a universal English, French and Arabic web-registration experience with complete right-to-left support for Arabic;
2. support passwordless applicant access through email verification;
3. support public entry and controlled organization-level invitation links;
4. collect a proportionate participant profile with conditional identity requirements;
5. provide draft saving, submission, status tracking and additional-information handling;
6. give authorized users a searchable and permission-controlled review workspace;
7. support direct internal qualification and assignment of role, badge and access;
8. generate, revoke and replace secure Digital Entry Passes;
9. manage pre-event generic Physical Badge production and event-time issuance by type;
10. support restricted identity and entry verification by internal or external security users using multiple approved methods;
11. provide operational reporting, controlled exports and complete audit history;
12. provide a restricted integration boundary for the separate mobile application.

## Non-Goals

V1 will not deliver:

- the ASC mobile application;
- agenda, session or live-program management;
- B2B matchmaking, messaging or translation;
- Deal Room reservation or confidential document exchange;
- stand selection, commercial invoicing or online payment;
- interactive venue maps;
- lead retrieval and contact export for exhibitors;
- VIP flight, hotel or transportation tracking;
- a full external delegation coordinator portal;
- automatic or AI-based badge recommendations;
- a full marketing-automation or business-intelligence platform;
- offline mobile-application functionality.

## Future Considerations

The following may be considered after V1 is stable and must not delay the core release:

- an external delegation coordinator workspace;
- optional SMS-based verification and additional notification channels;
- offline entry verification where venue connectivity requires it;
- advanced operational analytics;
- configurable qualification assistance that never replaces authorized human decisions;
- integration with future commercial or exhibitor-management modules.

# Scope

## V1 In-Scope Capabilities

| **Capability Area** | **Included Capabilities** |
| --- | --- |
| Public registration | Email verification, draft saving, universal form, conditional fields, submission and status tracking |
| Identity verification | Algerian NIN flow, international passport flow, manual-examination routing and data masking |
| Organizations and invitations | Canonical organization records, reusable invitation campaigns, validity controls, optional capacity and source tracking |
| Delegations | Delegation records, manual member addition, CSV import and individual completion links |
| Administrative processing | Search, filtering, review, internal notes, additional-information requests, decisions and reopening |
| Qualification | Internal participant role, badge type and access-profile assignment by authorized users |
| Authorization | Users, roles, groups, atomic permissions and restricted external accounts |
| Credential management | Badge Type Assignment, Digital Entry Pass generation, activation, revocation and replacement |
| Physical badge operations | Pre-event production quantities, stock by type and event-time issuance records |
| Security and entry | QR scanning, NIN, passport, Registration Reference or manual lookup, restricted identity display, access decision and check-in recording |
| Communications | Transactional email and SMS-ready integration |
| Reporting and exports | Operational dashboards, permission-controlled reports, encrypted exports and audit history |
| External integration | Official-site routing, NIN service, email, optional SMS, pre-event badge-production output and optional restricted mobile API |

## Out-of-Scope Capabilities

Capabilities listed as non-goals remain outside the product boundary even if they appear in broader ASC specifications. Requests that would add event-program, networking, commercial, venue-map, lead-retrieval or protocol-logistics functionality require a separate scope decision.

## Boundary with the Mobile Application

The registration platform owns the authoritative registration, accreditation, credential and access records. The separate mobile application is optional, is not required for the web release and may consume only approved information through a versioned read-only API.

| **May Be Exposed to the Mobile Application** | **Must Not Be Exposed** |
| --- | --- |
| Approved opaque integration identifier | NIN |
| Approved public profile where visibility is enabled | Passport number or passport image |
| Assigned public-facing role where permitted | Identity-card copy |
| Approved public-facing participation status where permitted | Internal review notes |
| Profile photograph where permitted | Verification-service response details |
| Networking visibility preference | Security assessment or restricted flags |
| Approved organization information | Audit records and administrative history |
| Approved profile contact fields for a defined purpose | Contact information without an approved purpose |

The mobile application must never connect directly to the registration database. Every exposed field must be allowlisted for a defined purpose, and public participant-directory visibility requires a separate optional preference. V1 does not depend on the mobile application for registration, Digital Entry Pass delivery, entry verification or QR display.

## Boundary with the Official ASC Website

The official website remains the public source for event information, program content, speakers, exhibitors and practical information. It will link to the registration platform and may display whether registration is open or closed. Controlled organization invitation links are distributed through authorized channels and are not published as general website navigation.

## Operational Interfaces

V1 contains three permission-based interfaces:

- **Public Registration Portal**: registration, draft management, status tracking, information completion and Digital Entry Pass retrieval;
- **Operations Back Office**: review, qualification, organizations, invitations, credential readiness, physical-badge stock and issuance, reporting and access administration;
- **Security and Entry Portal**: Digital Entry Pass scanning, approved identity lookup, minimum identity confirmation, access decision and check-in.

These interfaces share one backend and one authorized data model. They do not require separate databases.

# Stakeholders and User Classes

## Business Stakeholders

| **Stakeholder** | **Primary Interest** |
| --- | --- |
| ASC Organizing Committee | Delivery of a coherent registration and accreditation process aligned with conference operations |
| Registration Operations Team | Efficient intake, review, correction, qualification and participant support |
| Event Security Team | Reliable identity, credential and access verification with minimum necessary data exposure |
| Protocol and Delegation Coordination | Organization-linked registrations, delegation tracking and controlled invitations |
| Badge and Entry Operations | Accurate pre-event physical-badge production, stock, distribution, Digital Entry Pass control and entry-event recording |
| Technical Delivery Team | Clear product boundaries, stable requirements and defined integration responsibilities |
| Data Protection and Legal Functions | Proportionate collection, lawful processing, retention control and auditable access |
| Mobile Application Team | Stable access to approved non-sensitive participant information through a controlled API |

## Applicant User Class

An applicant is any person completing the universal registration form through open registration, an organization invitation, an internal completion link or an on-site process. Applicants provide factual information and do not choose an internal participant category.

Applicants need to:

- verify access to their email address;
- save and resume a draft;
- understand which information is required and why;
- submit identity, contact and professional information securely;
- respond to specific requests for additional information;
- track the public status of their registration;
- retrieve an approved Digital Entry Pass when enabled;
- request withdrawal or permitted corrections.

## Invited Organization Participant

This user follows the same universal registration journey as any applicant. The difference is internal: the registration is linked automatically to an invitation campaign and an inviting organization. The participant's actual organization is collected separately and may differ from the inviting organization.

The invitation link does not provide a Badge Type, Digital Entry Pass, Physical Badge, priority decision or automatic approval.

## Internal and External Operational Users

| **User Class** | **Primary Responsibilities** | **Access Principle** |
| --- | --- | --- |
| Registration Reviewer | Review applications and request additional information | Sees registration information required for review |
| Accreditation Manager | Decide applications and assign role, Badge Type and access | Executes qualification actions directly when permitted |
| Credential Operator | Generate, activate, revoke and replace Digital Entry Passes | Sees credential information without unnecessary identity documents |
| Badge Distribution Operator | Manage generic physical-badge stock and record issuance | Sees approved identity confirmation and assignment information only |
| Security Verification User | Perform permitted identity or security checks | Receives restricted access to necessary identity information |
| Entry Control User | Scan Digital Entry Passes or perform approved identity lookup and record entry outcomes | Sees photograph, name, credential state and access result only |
| Access Administrator | Manage users, roles, groups and permissions | Manages authorization without automatic access to participant documents |
| Platform Administrator | Manage platform configuration and technical operations | Technical administration does not imply unrestricted data access |
| Auditor or Oversight User | Review decisions, exports and audit events | Read-only access according to assigned scope |

A user may hold multiple roles directly or through groups. Special access is granted through an appropriate limited role rather than unmanaged individual exceptions.

## Internal Participant Classifications

The internal classification catalog may include:

- Government VIP;
- Organizer or Staff;
- Investor or VC;
- Startup Exhibitor;
- Speaker;
- Media;
- Visitor.

These values are internal outputs of the qualification process. They must not appear as applicant-selectable registration paths.

## External Systems

| **External System** | **Product Relationship** |
| --- | --- |
| NIN Verification Service | Verifies Algerian identity information and returns a minimum functional result |
| Email Delivery Service | Sends OTP messages and transactional notifications |
| SMS Service | Optional future verification and urgent-notification channel |
| Official ASC Website | Routes public visitors to registration and communicates registration availability |
| ASC Mobile Application | Optionally consumes approved non-sensitive participant data through a versioned read-only API |
| Physical Badge Producer | Produces approved generic Physical Badges by type before the event from print-ready artwork and quantity instructions |
| Authorized Security Recipient | Receives only explicitly authorized and auditable exports when required |

> **Approved Foundation**
>
> Sections 1 through 7 establish the approved product foundation. The following sections translate that foundation into user journeys, functional requirements, business rules and status models.

# Key User Journeys

## Journey Overview

| **ID** | **Journey** | **Primary Outcome** |
| --- | --- | --- |
| UJ-01 | Open Registration | A member of the public submits the universal registration form. |
| UJ-02 | Organization Invitation | A participant registers through a reusable organization invitation and is linked to its campaign. |
| UJ-03 | Save and Resume Draft | An applicant safely returns to an incomplete registration. |
| UJ-04 | Additional Information | An applicant completes a targeted correction or document request. |
| UJ-05 | Administrative Review | An authorized user reviews the application and determines the next action. |
| UJ-06 | Qualification and Assignment | An authorized user assigns the internal role, badge type and access profile. |
| UJ-07 | Credential Readiness and Physical Badge Issuance | The platform prepares contextual Digital Entry Passes and records distribution of generic Physical Badges. |
| UJ-08 | Entry Verification | Entry personnel identify the approved context through an accepted method and record an access event. |
| UJ-09 | Delegation Import | Authorized staff create a delegation and send individual completion links. |
| UJ-10 | Registration on Behalf of a Participant | Authorized staff create an initial record for another person. |
| UJ-11 | On-Site Registration | Authorized staff register a participant at the venue without bypassing mandatory checks. |
| UJ-12 | Withdrawal or Cancellation | An applicant or authorized user ends the registration or accreditation safely. |

## UJ-01 Open Registration

**Primary actor:** Applicant.

**Trigger:** The applicant selects the public registration link from the official ASC website or an authorized public communication.

**Preconditions:**

- public registration is open;
- the applicant has access to a valid email address;
- the platform can determine whether the applicant is recovering an existing context or creating a permitted new registration context.

**Main flow:**

1. The applicant enters an email address and receives a one-time verification code.
2. The platform verifies the code and creates an account and draft registration.
3. The applicant selects English, French or Arabic and completes the universal registration steps.
4. Conditional identity fields show the NIN flow for Algerian participants or the passport flow for international participants.
5. The applicant provides professional information, interests, participation objectives and required consent.
6. The platform displays a final review page without any participant-role or badge selection.
7. The applicant confirms accuracy and submits the registration.
8. The platform creates a submission snapshot, assigns a registration reference and shows the neutral public status **Submitted**.
9. The platform sends a confirmation message.

**Alternative and exception flows:**

- If the email is linked to an existing account, the applicant is directed to sign in and resume the relevant record or begin a permitted new registration context.
- If a strong identity match is detected, the platform links or routes the registration for authorized review; it prevents only an accidental duplicate within the same context.
- If NIN verification is unavailable or inconclusive, the registration may still be submitted and is marked for manual examination.
- If a mandatory field is invalid or missing, submission is blocked and the affected step is identified clearly.

## UJ-02 Organization Invitation

**Primary actor:** Invited participant.

**Trigger:** The participant opens a secure reusable link distributed by a ministry, organization, delegation or partner.

**Main flow:**

1. The platform validates that the invitation campaign is active, within its validity period and below its optional capacity.
2. The platform shows a neutral message identifying the organization through which the registration is being made.
3. The participant verifies an email address and completes the same universal form used for open registration.
4. The platform records the invitation campaign, inviting organization and registration source automatically.
5. The participant provides the actual organization separately; it may differ from the inviting organization.
6. Submission follows the same validation, identity and review rules as open registration.

**Rules:**

- the link never grants approval, priority, role, badge or access rights;
- a suspended, expired, closed or capacity-complete campaign cannot create a new registration;
- registrations already created through a campaign remain traceable if the campaign is later suspended;
- forwarded links do not prove membership in the inviting organization.
- a person already registered through another source may create a separate registration linked to this invitation campaign without duplicating the underlying person profile.

## UJ-03 Save and Resume Draft

**Primary actor:** Applicant.

1. The platform saves completed steps automatically and when the applicant explicitly continues.
2. The applicant may leave the platform after a verified account has been created.
3. On return, the applicant verifies access and sees the existing draft rather than a new form.
4. The platform displays completion progress and identifies missing mandatory information.
5. The applicant edits any draft field and continues to submission.

Draft expiry and reminders will be configurable. Expiration must not silently convert a draft into a submitted application.

## UJ-04 Additional Information

**Primary actors:** Registration Reviewer and applicant.

1. An authorized reviewer selects one or more fields or document requests requiring correction or completion.
2. The reviewer writes a clear applicant-facing request and may define an optional response deadline.
3. The platform changes the public status to **Additional Information Required** and sends a notification.
4. The applicant signs in and sees only the requested items and relevant context.
5. The applicant updates the permitted fields, uploads requested evidence where necessary and resubmits.
6. The platform preserves previous values, records the change history and returns the application to review.

Changes to verified email, NIN or passport information trigger the applicable verification process again.

## UJ-05 Administrative Review

**Primary actor:** Authorized operational user.

1. The user opens a work queue or searches for an application.
2. The platform displays the applicant record, registration source, organization relationships, identity-verification result, duplicate indicators and relevant supporting information.
3. The user may add an internal note, request information, correct an authorized field, flag a duplicate or proceed to a decision.
4. A service failure or unidentified external result is shown as an exception, not as a successful verification or automatic rejection.
5. Every action takes effect immediately when the user holds the required permission and is recorded in the audit log.

The reviewer does not need to download all identity documents to perform ordinary review. Sensitive files remain hidden unless the user holds the specific permission and the document is required.

## UJ-06 Qualification and Assignment

**Primary actor:** Accreditation Manager or another authorized user.

1. The user records an accreditation decision.
2. For an approved application, the user assigns a Participant Role, Badge Type and Access Profile as three separate values.
3. The platform may present configurable defaults, but the values are not committed automatically.
4. The user records a reason or internal note where required.
5. The decision takes effect immediately and the public status is updated.
6. The platform sends a neutral applicant notification based on the final decision and configured communication rules.

The applicant never supplies these assignments through the public interface or public API.

## UJ-07 Credential Readiness and Physical Badge Issuance

**Primary actor:** Credential Operator, Badge Distribution Operator or another authorized user.

1. An approved context receives separate Participant Role, Badge Type and Access Profile Assignments.
2. The platform creates a contextual Digital Entry Pass containing an opaque QR value or Registration Reference and no readable personal identity data in the credential payload.
3. The participant may view, download or print the Digital Entry Pass according to policy.
4. Generic Physical Badges are produced in batches by Badge Type before the event and entered into stock as quantities rather than personalized credentials.
5. At the distribution desk, an authorized user locates the approved context, confirms identity as required and records a Physical Badge Issuance Event with the issued Badge Type, time, operator and location.
6. When a Digital Entry Pass must be replaced, the platform invalidates the previous pass for that context before activating the replacement; other valid contexts remain unaffected.

One person may hold multiple active Badge Type Assignments and Digital Entry Passes, including different Badge Types and Access Profiles, when authorized users have assigned them to valid participation contexts. A generic Physical Badge is not a personalized identity credential.

## UJ-08 Entry Verification

**Primary actor:** Entry Control User.

1. The user opens the restricted Security and Entry Portal.
2. The user scans the Digital Entry Pass QR code or searches by NIN, passport number, Registration Reference or another authorized manual criterion.
3. The platform locates the person and requires selection of the exact approved context when more than one eligible context exists.
4. The platform validates the selected Digital Entry Pass where applicable, the linked accreditation, its Access Profile and any active person-level security restriction for the selected checkpoint or zone.
5. The interface shows only the photograph, name, selected context, credential state and access decision required for entry.
6. The user records entry or an authorized denial reason.
7. The platform stores the event with checkpoint, time, user, verification method and selected context or Digital Entry Pass reference.

Revoked, replaced, expired or unknown Digital Entry Passes must produce a clear non-admission result without exposing sensitive identity details. A generic Physical Badge alone must not be accepted as secure identity proof at a controlled checkpoint.

The access decision is based on the exact approved context selected for the verification. The system does not merge privileges from other contexts held by the same person. An active person-level security restriction overrides every context linked to that person.

## UJ-09 Delegation Import

**Primary actor:** Authorized operations user.

1. The user creates a delegation linked to a canonical organization and country.
2. The user adds members manually or imports an approved CSV template.
3. The platform validates required columns and detects existing person profiles, related registration contexts or repeated members.
4. For valid new members, the platform creates individual invitation records and completion links.
5. Each member completes the universal registration form individually.
6. Operations users monitor completion without receiving an external coordinator-editing portal in V1.

## UJ-10 Registration on Behalf of a Participant

**Primary actor:** Authorized internal user.

1. The user creates an initial participant and registration record.
2. The platform records the creator, creation source and primary contact separately from the participant.
3. When possible, the participant receives a secure link to complete and confirm the information.
4. When participant self-completion is not operationally possible, the authorized user may complete the permitted fields and record the information source.
5. The application remains subject to the same identity, qualification, credential and physical-badge rules.

## UJ-11 On-Site Registration

**Primary actor:** Authorized on-site user.

The user creates or locates a participant record, captures the minimum required information, performs available identity and duplicate checks, records the source as `ON_SITE` and proceeds according to the user's permissions. On-site registration may be operationally expedited but must not bypass mandatory checks or audit requirements. It operates against the cloud by default; offline creation is available only when the optional Event Edge is approved and active. Otherwise, cloud unavailability activates a controlled manual contingency until service returns.

## UJ-12 Withdrawal or Cancellation

An applicant may request withdrawal. An authorized user may cancel a registration, accreditation, Badge Type Assignment or Digital Entry Pass according to permission scope. Withdrawal or cancellation:

- stops further ordinary processing;
- revokes the affected accreditation and Digital Entry Pass records when applicable without revoking unrelated valid contexts automatically;
- prevents further entry through the affected context or Digital Entry Pass;
- records actor, time, reason and resulting status;
- does not automatically delete data that remains subject to an approved retention obligation.

# Functional Requirements

Requirements use the priority levels **Must**, **Should** and **Could**. A Must requirement is required for the V1 operational release. Should and Could requirements remain within product scope but may be sequenced according to the implementation plan.

## Authentication and Account Management

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| FR-AUTH-001 | Must | The platform shall allow an applicant to authenticate through a verified email address and a one-time code without requiring a password. |
| FR-AUTH-002 | Must | A one-time code shall expire after a configurable period, be usable once and be invalidated when a replacement code is issued. |
| FR-AUTH-003 | Must | The platform shall limit repeated code requests and verification attempts by account, destination and technical risk signals. |
| FR-AUTH-004 | Must | The platform shall create an immutable internal account identifier that is independent from the email address. |
| FR-AUTH-005 | Must | A returning applicant shall be directed to the existing account and the relevant registration context rather than creating an accidental duplicate draft for the same context. |
| FR-AUTH-006 | Must | Changing a verified email address shall require reverification and shall preserve the account and registration history. |
| FR-AUTH-007 | Must | The platform shall support explicit sign-out and configurable session expiration. |
| FR-AUTH-008 | Must | Internal and external operational users shall authenticate through an administrative authentication flow separate from applicant email OTP. |
| FR-AUTH-009 | Must | The administrative authentication flow shall enforce MFA for privileged operational accounts and support future SSO integration without changing product authorization rules. |
| FR-AUTH-010 | Must | External security accounts shall support start date, expiry date, activation and immediate deactivation. |
| FR-AUTH-011 | Must | Disabled or expired operational accounts shall be denied access to every interface and API. |
| FR-AUTH-012 | Must | Successful and failed operational authentication events shall be auditable without storing secrets or OTP values. |
| FR-AUTH-013 | Must | V1 shall maintain at most one active Participant Account per Person while allowing multiple verified email addresses to be enabled as login destinations for that account. |

## Universal Registration

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| FR-REG-001 | Must | The platform shall provide one universal registration form for open, invited, delegation, internal and on-site sources. |
| FR-REG-002 | Must | The applicant interface and public API shall not expose a participant-role, badge-type or access-profile selection field. |
| FR-REG-003 | Must | The platform shall create a draft only after account verification or an authorized internal creation action. |
| FR-REG-004 | Must | Every registration shall receive an immutable internal identifier and a human-readable registration reference. |
| FR-REG-005 | Must | The form shall be divided into clear steps with progress indication and previous-step navigation. |
| FR-REG-006 | Must | The platform shall save valid draft changes and allow the applicant to resume from the last completed step. |
| FR-REG-007 | Must | Draft data shall remain editable until final submission. |
| FR-REG-008 | Must | The platform shall identify missing or invalid required information at the relevant field and step. |
| FR-REG-009 | Must | Conditional questions shall be displayed only when applicable to nationality, residence, identity or previous answers. |
| FR-REG-010 | Must | The applicant shall receive a complete review step before submission. |
| FR-REG-011 | Must | Submission shall require confirmation of data accuracy and acceptance of mandatory processing notices. |
| FR-REG-012 | Must | Marketing consent shall be separate, optional and must not block registration. |
| FR-REG-013 | Must | Final submission shall create an auditable snapshot of the submitted values. |
| FR-REG-014 | Must | After submission, applicant editing shall be limited to fields returned through an additional-information request or another permitted correction flow. |
| FR-REG-015 | Must | The applicant shall be able to view a neutral public status and the registration reference. |
| FR-REG-016 | Must | The applicant shall be able to request withdrawal. |
| FR-REG-017 | Must | The public form shall support English, French and Arabic content, including right-to-left layout for Arabic, while technical field identifiers remain in English. |
| FR-REG-018 | Must | The form shall be usable on current desktop and mobile web browsers without requiring the ASC mobile application. |
| FR-REG-019 | Should | Draft reminders and draft-expiration rules shall be configurable and shall never submit a draft automatically. |
| FR-REG-020 | Must | One person may hold multiple registration records when they represent distinct invitation, organization, delegation or participation contexts. |

## Personal and Contact Information

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| FR-PER-001 | Must | The platform shall collect first name and last name using the character and length rules defined in the field catalog. |
| FR-PER-002 | Should | The platform shall support optional first-name and last-name fields in the participant's native language. |
| FR-PER-003 | Must | The platform shall collect gender and date of birth where required for identity, accreditation or security processing. |
| FR-PER-004 | Must | The platform shall collect nationality, country of residence and city of residence. |
| FR-PER-005 | Must | The platform shall collect a verified account email. An international-format mobile number is required for Algerian participants and optional for international participants unless an approved operational policy requires it. |
| FR-PER-006 | Must | The applicant shall select a preferred communication language from the supported languages. |
| FR-PER-007 | Must | The platform shall collect an official profile photograph that satisfies configurable format, size and identity-confirmation quality rules. |
| FR-PER-008 | Must | The platform shall validate uploaded photograph type and size before accepting it. |
| FR-PER-009 | Must | Full residential address, marital status, banking data and unrelated personal data shall not be collected in V1. |
| FR-PER-010 | Must | Authorized interfaces shall mask contact and identity values according to the viewer's permissions. |

## Identity Verification

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| FR-IDV-001 | Must | The platform shall apply the Algerian NIN flow when the approved nationality or participant-context rule requires it. |
| FR-IDV-002 | Must | NIN shall be stored and validated as an 18-character string and never as a numeric value. |
| FR-IDV-003 | Must | The platform shall validate NIN format before calling an external verification service. |
| FR-IDV-004 | Must | When available, the platform shall submit the minimum required values to the approved NIN verification service. |
| FR-IDV-005 | Must | The platform shall retain only the minimum verification status, reference, timestamp and approved comparison result rather than the complete external response. |
| FR-IDV-006 | Must | Successful NIN verification shall remove the default requirement for an Algerian identity-card copy. |
| FR-IDV-007 | Must | An unavailable, unidentified, invalid or inconclusive verification response shall be routed to manual examination and shall not be interpreted as automatic approval or rejection. |
| FR-IDV-008 | Must | The platform shall apply the passport flow to international participants according to the approved business rule. |
| FR-IDV-009 | Must | The passport flow shall collect passport number, issuing country and expiry date. |
| FR-IDV-010 | Must | A passport identity-page copy shall be requested only when required by the approved policy or a targeted review request. |
| FR-IDV-011 | Must | NIN and passport values shall be masked by default in operational interfaces. |
| FR-IDV-012 | Must | Changing NIN, passport number, issuing country, expiry date or verified email shall invalidate the prior verification state and start reverification. |
| FR-IDV-013 | Must | Authorized users shall be able to record a manual verification result, reason and evidence reference without overwriting external verification history. |
| FR-IDV-014 | Must | Identity documents shall be visible only to users holding the specific document-access permission. |
| FR-IDV-015 | Must | The platform shall record identity-verification attempts without writing full identity values or document contents to general application logs. |

## Professional Profile Interests and Objectives

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| FR-PRO-001 | Must | The platform shall collect organization name, organization country, organization type and job title. |
| FR-PRO-002 | Should | The platform shall collect an organization website and, optionally, a professional profile URL. |
| FR-PRO-003 | Must | The platform shall support a configurable short professional biography. |
| FR-PRO-004 | Must | The applicant shall be able to select multiple areas of interest from a configurable taxonomy. |
| FR-PRO-005 | Must | The applicant shall select or describe participation objectives using neutral action-oriented choices. |
| FR-PRO-006 | Must | Professional-profile answers shall support internal review but shall not assign a participant role automatically. |
| FR-PRO-007 | Should | The applicant may provide one supporting professional document when requested or operationally relevant. |
| FR-PRO-008 | Must | Organization type, interests and objectives shall be configurable without source-code changes. |

## Organization Registry

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| FR-ORG-001 | Must | The platform shall maintain a canonical organization registry used by invitations, delegations and participant affiliations. |
| FR-ORG-002 | Must | An organization record shall include a canonical name, type, country and active status. |
| FR-ORG-003 | Should | An organization may include alternate names, official identifier, website, department and contact information when approved. |
| FR-ORG-004 | Must | Authorized users shall search and reuse an existing organization before creating a new record. |
| FR-ORG-005 | Must | The platform shall warn about possible organization duplicates and shall not merge them automatically. |
| FR-ORG-006 | Must | Organization creation, edits, deactivation and controlled consolidation shall be permission-restricted and auditable. |
| FR-ORG-007 | Must | Deactivating an organization shall not remove historical invitation, registration or delegation relationships. |
| FR-ORG-008 | Should | Organization taxonomy values shall be configurable. |

## Organization Invitation Campaigns

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| FR-INV-001 | Must | Authorized users shall create an invitation campaign linked to one canonical inviting organization. |
| FR-INV-002 | Must | A campaign shall have a name, internal reference, status and secure non-guessable link code. |
| FR-INV-003 | Must | A campaign link shall be reusable by multiple participants until suspended, expired, closed or capacity-complete. |
| FR-INV-004 | Should | A campaign may define optional validity start and end dates. |
| FR-INV-005 | Should | A campaign may define an optional maximum registration count. |
| FR-INV-006 | Should | A campaign may define an optional department, delegation, contact person and permitted email-domain signal. |
| FR-INV-007 | Must | The platform shall validate campaign availability before creating a registration through its link. |
| FR-INV-008 | Must | Registrations created through a campaign shall record the campaign, inviting organization and source automatically. |
| FR-INV-009 | Must | The participant's actual organization shall be stored separately from the inviting organization. |
| FR-INV-010 | Must | Campaign use shall not assign role, badge, access, priority or approval. |
| FR-INV-011 | Must | Authorized users shall activate, suspend, close or expire a campaign without deleting registrations already created. |
| FR-INV-012 | Must | The platform shall report campaign starts, drafts, submissions, review statuses and outcomes. |
| FR-INV-013 | Must | A campaign link shall not contain personal data or reveal internal authorization information. |
| FR-INV-014 | Should | Authorized users shall be able to regenerate the public link code while preserving the campaign record and history. |
| FR-INV-015 | Must | An inviting organization shall not receive participant-data access merely because registrations are linked to its campaign. |

## Delegations

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| FR-DEL-001 | Must | Authorized users shall create a delegation linked to an organization and country. |
| FR-DEL-002 | Must | A delegation shall support an internal reference, optional coordinator contact and active status. |
| FR-DEL-003 | Must | Authorized users shall add members individually or through an approved CSV import template. |
| FR-DEL-004 | Must | Import validation shall identify missing required columns, invalid values and duplicate member candidates before creating records. |
| FR-DEL-005 | Must | Each valid new member shall receive an individual registration or completion link. |
| FR-DEL-006 | Must | Delegation members shall complete the same universal registration form and remain subject to ordinary review. |
| FR-DEL-007 | Must | Authorized users shall monitor member invitation, draft, submission and decision status. |
| FR-DEL-008 | Must | V1 shall not allow an external delegation coordinator to edit all participant identity data. |
| FR-DEL-009 | Should | Authorized users shall export a delegation completion report that excludes unnecessary sensitive identity values. |

## Application Management

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| FR-APP-001 | Must | Authorized users shall view application queues according to permission scope. |
| FR-APP-002 | Must | The platform shall support search by registration reference and permitted participant, organization, campaign, delegation, assignment and credential attributes. |
| FR-APP-003 | Must | The platform shall support filters for public status, internal status, verification state, country, organization, campaign, delegation, role, Badge Type and Digital Entry Pass state. |
| FR-APP-004 | Should | An application may be assigned to a user or work group without restricting access for other authorized users. |
| FR-APP-005 | Must | Authorized users shall view the submitted snapshot and subsequent applicant changes. |
| FR-APP-006 | Must | Authorized users shall add internal notes that are never visible to applicants unless copied explicitly into an applicant-facing request. |
| FR-APP-007 | Must | Authorized users shall create a targeted additional-information request identifying the fields or documents that may be updated. |
| FR-APP-008 | Should | An additional-information request may define an optional response deadline. |
| FR-APP-009 | Must | Authorized users shall correct fields within permission scope, with previous and new values recorded. |
| FR-APP-010 | Must | The platform shall flag potential duplicates using identity, email, mobile and organizational signals. |
| FR-APP-011 | Must | The platform shall not merge or delete potential duplicates automatically. |
| FR-APP-012 | Must | Authorized users shall resolve a potential match by linking registrations to the same person, confirming separate persons or identifying an accidental duplicate within the same context, with the resolution reason recorded. |
| FR-APP-013 | Must | Authorized users shall reopen an application according to permission scope while preserving the prior decision and status history. |
| FR-APP-014 | Must | The application detail shall show registration source, inviting organization, participant organization, campaign and delegation relationships. |
| FR-APP-015 | Must | Every application action shall be authorized and auditable. |
| FR-APP-016 | Must | Authorized users shall view the other registration, invitation, accreditation and credential contexts linked to the same person according to permission scope. |

## Qualification and Decision Management

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| FR-ACC-001 | Must | Authorized users shall record an accreditation decision as approved or not approved. |
| FR-ACC-002 | Must | Approval shall require assignment of a Participant Role, Badge Type and Access Profile unless an approved exception rule applies. |
| FR-ACC-003 | Must | Participant Role, Badge Type and Access Profile shall be stored as separate attributes. |
| FR-ACC-004 | Should | The platform may display configurable default mappings, but shall not commit them without an authorized action. |
| FR-ACC-005 | Must | The platform shall never accept applicant-supplied classification values from the public client or API. |
| FR-ACC-006 | Must | Decision and assignment actions shall take effect immediately when the user holds the required permission. |
| FR-ACC-007 | Must | V1 shall not require a secondary approval step for qualification actions. |
| FR-ACC-008 | Must | The user shall provide a reason or internal note for configured sensitive decisions and later changes. |
| FR-ACC-009 | Must | Authorized users shall change an approved assignment while preserving the previous values and actor history. |
| FR-ACC-010 | Must | Changing an assignment shall trigger Digital Entry Pass and physical-badge readiness rules for every affected context without changing unrelated contexts held by the person. |
| FR-ACC-011 | Must | A public decision notification shall use neutral approved templates and shall not expose internal notes. |
| FR-ACC-012 | Must | Not-approved records shall remain available to authorized users according to retention policy. |
| FR-ACC-013 | Must | Withdrawal or cancellation after approval shall revoke the affected accreditation and its related Digital Entry Pass without revoking unrelated valid contexts automatically. |
| FR-ACC-014 | Must | The platform shall show the current decision and complete prior-decision history to authorized users. |

## Roles Groups and Permissions

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| FR-RBAC-001 | Must | The platform shall define atomic permissions for protected actions and data views. |
| FR-RBAC-002 | Must | A role shall consist of one or more permissions. |
| FR-RBAC-003 | Must | A group shall contain users and receive one or more roles. |
| FR-RBAC-004 | Must | A user may receive multiple roles directly or through multiple groups. |
| FR-RBAC-005 | Must | Permission evaluation shall combine allowed roles without creating an implicit administrator bypass. |
| FR-RBAC-006 | Must | The backend shall enforce permissions independently of interface visibility. |
| FR-RBAC-007 | Must | Interfaces shall hide or disable actions that the current user cannot perform. |
| FR-RBAC-008 | Must | Platform administration shall not automatically grant access to participant documents or sensitive identity values. |
| FR-RBAC-009 | Must | External security users shall receive limited roles rather than general back-office access. |
| FR-RBAC-010 | Must | User, role, group and permission changes shall be auditable. |
| FR-RBAC-011 | Must | Deactivating a user shall terminate new access without removing historical attribution. |
| FR-RBAC-012 | Should | Authorized administrators shall clone an existing role as a starting point for a controlled custom role. |
| FR-RBAC-013 | Must | Direct individual permissions shall not be used when an appropriate limited role can represent the requirement. |
| FR-RBAC-014 | Must | Sensitive exports, identity documents, accreditation decisions, Digital Entry Pass actions, Physical Badge actions and entry actions shall have distinct permissions. |
| FR-RBAC-015 | Must | Technical-support and access-administration roles shall use masked operational data by default and shall not imply unrestricted participant-data access. |

## Credential and Physical Badge Management

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| FR-BDG-001 | Must | An approved Registration Context shall support separate Participant Role, Badge Type and Access Profile Assignments, with no more than one current active assignment in each category. |
| FR-BDG-002 | Must | A person may hold multiple active Badge Type Assignments when each assignment belongs to an authorized registration or accreditation context. |
| FR-BDG-003 | Must | The platform shall generate a Digital Entry Pass only for an eligible approved context with the required assignments. |
| FR-BDG-004 | Must | Every Digital Entry Pass shall have an immutable identifier and independently managed credential versions. |
| FR-BDG-005 | Must | A Digital Entry Pass QR value shall be opaque, random or signed and shall contain no readable personal identity data or direct internal record identifier. |
| FR-BDG-006 | Should | An approved participant may view, download and print the Digital Entry Pass according to configured policy. |
| FR-BDG-007 | Must | Replacing a Digital Entry Pass shall invalidate the prior credential version for that context before the replacement becomes active, without invalidating other valid contexts held by the person. |
| FR-BDG-008 | Must | Digital Entry Pass replacement or revocation shall require an authorized reason and shall retain history. |
| FR-BDG-009 | Must | Official Physical Badges shall be generic by Badge Type and shall not display a participant name, photograph, NIN, passport number or personalized QR code. |
| FR-BDG-010 | Must | Generic Physical Badges shall be produced in approved batches before the event; live personalized badge printing at entry shall not be required. |
| FR-BDG-011 | Must | The platform shall track planned, produced, available and issued quantities by Badge Type without requiring a unique record for every generic badge item. |
| FR-BDG-012 | Must | An authorized user shall record a Physical Badge Issuance Event against the exact person and approved context after the required identity confirmation. |
| FR-BDG-013 | Must | An issuance event shall record Badge Type, person, context, time, operator and distribution point. |
| FR-BDG-014 | Must | A replacement physical badge shall create a new issuance event with a reason and shall not create or modify a personalized QR credential. |
| FR-BDG-015 | Must | Badge Type Assignment, Digital Entry Pass state, physical-badge stock and Physical Badge Issuance Events shall be auditable and independently reportable. |
| FR-BDG-016 | Must | Each Participant Role, Badge Type and Access Profile Assignment and each Digital Entry Pass shall retain its source Registration Context and inviting organization where applicable. |
| FR-BDG-017 | Must | Changing role, Badge Type or Access Profile shall identify the affected Digital Entry Pass and physical-badge readiness actions without changing unrelated contexts automatically. |

## Security and Entry Management

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| FR-ENT-001 | Must | The Security and Entry Portal shall allow authorized users to scan Digital Entry Pass QR values using a supported web-device camera. |
| FR-ENT-002 | Must | The portal shall resolve a QR value to the current Digital Entry Pass and approved context without exposing the token in general logs. |
| FR-ENT-003 | Must | The portal shall support authorized lookup by NIN, passport number, Registration Reference and approved manual criteria in addition to Digital Entry Pass scanning. |
| FR-ENT-004 | Must | When more than one eligible context exists for a person, the user shall select the exact context before the access decision is recorded. |
| FR-ENT-005 | Must | Entry validation shall consider the selected context, applicable Digital Entry Pass state, Access Profile, checkpoint or zone and any active person-level security restriction. |
| FR-ENT-006 | Must | Authorized users shall record a successful check-in event. |
| FR-ENT-007 | Should | Authorized users may record an entry denial reason from a configurable list. |
| FR-ENT-008 | Must | Every entry event shall record time, checkpoint, verification method, selected context and acting user or device context. |
| FR-ENT-009 | Must | The portal shall display only the participant photograph, name, selected context, credential state and access outcome required for entry. |
| FR-ENT-010 | Must | Every lookup method shall follow the same data-minimization, permission and audit rules. |
| FR-ENT-011 | Should | The portal shall indicate a recent prior check-in without blocking an authorized new event automatically. |
| FR-ENT-012 | Must | External security accounts shall not gain access to professional documents, internal notes or bulk participant exports through the entry interface. |
| FR-ENT-013 | Must | Entry privileges shall be determined from the selected approved context and shall not inherit or merge privileges from another context held by the same person. |
| FR-ENT-014 | Must | An active person-level security restriction created by an authorized user shall deny entry through every context linked to that person. |
| FR-ENT-015 | Must | A generic Physical Badge alone shall not be accepted as secure identity proof at a controlled checkpoint. |
| FR-ENT-016 | Must | Lookup by NIN or passport number shall normally match the stored approved record and shall not call the external verification service for every entry event. |
| FR-ENT-017 | Must | The portal shall show a clear non-admission or escalation result for unknown, revoked, replaced, expired, restricted or unauthorized contexts without exposing sensitive identity details. |
| FR-ENT-018 | Must | External security users shall receive the access outcome without the restricted reason for a person-level security restriction unless a separate permission explicitly allows it. |

## Notifications

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| FR-NOT-001 | Must | The platform shall send transactional notifications using approved English, French and Arabic templates. |
| FR-NOT-002 | Must | Required email events shall include OTP, submission confirmation, additional-information request and final decision. |
| FR-NOT-003 | Should | Digital Entry Pass availability and organization invitation delivery shall use configurable templates. |
| FR-NOT-004 | Must | Pre-decision notifications shall remain neutral and shall not imply a participant role or badge. |
| FR-NOT-005 | Must | Notifications shall not include NIN, passport number, identity-document links or unrestricted personal data. |
| FR-NOT-006 | Must | The platform shall record notification type, destination, language, send time and delivery outcome. |
| FR-NOT-007 | Must | Failed transactional notifications shall support controlled retry without duplicating successful sends unnecessarily. |
| FR-NOT-008 | Must | OTP messages shall use a separate security template and handling path from ordinary notifications. |
| FR-NOT-009 | Should | SMS shall be supported through an optional provider integration without making it mandatory for initial applicant authentication. |
| FR-NOT-010 | Could | Configurable draft and deadline reminders may be activated after operational policy is approved. |
| FR-NOT-011 | Must | Marketing communication shall require separate optional consent and shall not reuse mandatory transactional consent. |

## Reporting and Dashboards

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| FR-REP-001 | Must | Authorized users shall view registration counts by public and internal status. |
| FR-REP-002 | Must | Authorized users shall filter permitted reports by date, country, organization, campaign and delegation. |
| FR-REP-003 | Must | Campaign reporting shall show starts, drafts, submissions, review states and outcomes. |
| FR-REP-004 | Must | Delegation reporting shall show member invitation and completion status. |
| FR-REP-005 | Must | Operational dashboards shall show review backlog and additional-information workload. |
| FR-REP-006 | Must | Credential reporting shall show Badge Type Assignments and Digital Entry Pass states by type and registration context where permitted. |
| FR-REP-007 | Must | Entry reporting shall show permitted checkpoint and check-in counts without exposing unnecessary identity values. |
| FR-REP-008 | Should | Authorized users shall view duplicate and identity-verification exception counts. |
| FR-REP-009 | Must | Report visibility and fields shall respect the viewer's permissions. |
| FR-REP-010 | Must | Dashboard totals shall link to permitted filtered records where operationally appropriate. |
| FR-REP-011 | Must | Reporting shall distinguish Unique Persons, Registration Records, Accreditation Records, Badge Type Assignments, Digital Entry Passes, Physical Badge Issuance Events and Entry Events. |
| FR-REP-012 | Must | Organization reporting shall preserve attribution to each inviting organization even when one person has registrations or Badge Type Assignments linked to multiple organizations. |

## Controlled Exports

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| FR-EXP-001 | Must | Every export type shall require an explicit permission. |
| FR-EXP-002 | Must | Export templates shall define an approved field set and shall not inherit all visible fields automatically. |
| FR-EXP-003 | Must | Security exports containing sensitive identity values shall use a distinct restricted permission. |
| FR-EXP-004 | Must | Sensitive exports shall be encrypted or transferred through an approved protected mechanism. |
| FR-EXP-005 | Must | Export files shall have a configurable availability period and shall not remain indefinitely downloadable. |
| FR-EXP-006 | Must | The platform shall record export requester, time, type, filters, field set and retrieval outcome. |
| FR-EXP-007 | Must | Identity documents shall not be included in tabular exports by default. |
| FR-EXP-008 | Should | Approved operational exports shall support CSV and spreadsheet-compatible formats. |
| FR-EXP-009 | Must | An export shall reflect the requester's permissions at generation time. |
| FR-EXP-010 | Must | Export generation shall not expose sensitive values in job names, URLs or general logs. |

## Audit Log

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| FR-AUD-001 | Must | The platform shall create an audit event for every configured sensitive administrative, authorization, export, credential, Physical Badge and entry action. |
| FR-AUD-002 | Must | An audit event shall record actor, timestamp, action, affected entity and result. |
| FR-AUD-003 | Must | Change events shall record previous and new values or an approved non-sensitive representation. |
| FR-AUD-004 | Must | Actions requiring a reason shall store that reason with the audit event. |
| FR-AUD-005 | Must | Ordinary users shall not modify or delete audit events. |
| FR-AUD-006 | Must | Authorized oversight users shall search and filter audit events by date, actor, action and entity. |
| FR-AUD-007 | Must | Audit events shall avoid storing OTP values, complete identity documents or unnecessary full identity numbers. |
| FR-AUD-008 | Must | User deactivation shall not remove historical actor attribution. |
| FR-AUD-009 | Must | Viewing or exporting restricted audit records shall itself be auditable. |
| FR-AUD-010 | Should | The platform shall support an approved audit-retention policy separate from ordinary application-document retention. |

# Business Rules

| **ID** | **Rule Area** | **Business Rule** |
| --- | --- | --- |
| BR-REG-001 | Neutral Registration | An applicant shall never select or request a Participant Role, Badge Type or Access Profile. |
| BR-REG-002 | Universal Form | All public and invited applicants use the same universal registration data model and core form. |
| BR-REG-003 | Submission | A submitted application preserves an immutable snapshot of the values confirmed by the applicant. |
| BR-REG-004 | Post-Submission Change | Applicant changes after submission occur only through an authorized correction or additional-information flow. |
| BR-INV-001 | Invitation Meaning | An organization invitation identifies registration source and context; it does not grant accreditation. |
| BR-INV-002 | Organization Separation | Inviting Organization and Participant Organization are separate relationships and may contain different organizations. |
| BR-INV-003 | Shared Link | A campaign link may be used by multiple participants but does not prove affiliation. |
| BR-INV-004 | No Automatic Organization Access | An invitation relationship does not grant the inviting organization access to participant records. |
| BR-IDV-001 | NIN Storage | NIN is an 18-character string and is never processed as a numeric value. |
| BR-IDV-002 | Document Reduction | Successful NIN verification removes the default need for an Algerian identity-card copy. |
| BR-IDV-003 | Inconclusive Result | An unavailable, unidentified or inconclusive verification result requires examination and is not success or rejection. |
| BR-IDV-004 | Sensitive Change | Changing verified identity values invalidates the prior verification result. |
| BR-DUP-001 | Identity Match | A strong identity match links or routes the registration to the existing person profile; it prevents only an accidental duplicate within the same registration context. |
| BR-DUP-002 | No Automatic Merge | Potential duplicate participant, application or organization records are never merged or deleted automatically. |
| BR-DUP-003 | Context Preservation | The same person may have multiple valid registration records when their invitation, organization, delegation or participation contexts differ. |
| BR-ACC-001 | Human Decision | The final accreditation decision and classification are performed by an authorized human user in V1. |
| BR-ACC-002 | Separate Assignment | Participant Role, Badge Type and Access Profile are distinct attributes. |
| BR-ACC-003 | Direct Action | A user holding the required permission executes the action directly without a secondary approval step. |
| BR-ACC-004 | Classification Source | Applicant-provided data informs review but never becomes a committed classification automatically. |
| BR-RBAC-001 | Least Privilege | Users receive access through the minimum appropriate roles and groups. |
| BR-RBAC-002 | Backend Enforcement | Hiding an interface action is not sufficient; every protected backend action enforces permission independently. |
| BR-RBAC-003 | Administration Separation | Technical or access administration does not imply unrestricted participant-data access. |
| BR-BDG-001 | Multiple Active Assignments | A person may hold multiple active Participant Role, Badge Type and Access Profile Assignments and Digital Entry Passes through different Registration Contexts; each retains an independent lifecycle and audit history. |
| BR-BDG-002 | Contextual Pass Replacement | Activating a replacement invalidates only the previous Digital Entry Pass credential version for the same context and does not invalidate other contexts held by the person. |
| BR-BDG-003 | QR Privacy | Digital Entry Pass QR values contain no readable NIN, passport number or direct personal profile. |
| BR-BDG-004 | Generic Physical Badge | The official Physical Badge is generic by Badge Type and contains no participant name, photograph or personalized QR code. |
| BR-BDG-005 | Pre-Event Production | Official Physical Badges are produced in batches before the event and distributed from controlled stock. |
| BR-BDG-006 | Separate Issuance Event | Giving a Physical Badge to a participant creates an issuance event and does not create a personalized physical credential. |
| BR-ENT-001 | Context-Specific Admission | Entry is evaluated using the exact selected approved context; privileges from other contexts held by the person are not merged. |
| BR-ENT-002 | Minimum Display | Entry users see only the minimum information required to confirm identity and access. |
| BR-ENT-003 | Person-Level Restriction | An active person-level security restriction overrides all contexts linked to that person. |
| BR-ENT-004 | Identity Methods | Entry verification may use a Digital Entry Pass, NIN, passport number, Registration Reference or authorized manual lookup. |
| BR-ENT-005 | Physical Badge Limitation | A generic Physical Badge alone is not secure identity proof for a controlled access decision. |
| BR-DATA-001 | Mobile Boundary | The mobile application receives no NIN, passport number, identity document, internal note or restricted security result. |
| BR-DATA-002 | Logging | Sensitive identity values, OTP values and identity-document contents do not appear in general application logs. |
| BR-DATA-003 | Withdrawal | Withdrawal or cancellation stops processing and access but does not override approved retention obligations. |
| BR-DATA-004 | Public Profile Choice | Public visibility in the separate mobile application or a participant directory requires a distinct optional preference. |
| BR-EXP-001 | Export Authorization | Every export is permission-controlled, field-limited, time-limited where applicable and auditable. |
| BR-AUD-001 | Traceability | Sensitive actions retain actor attribution even after the actor account is deactivated. |
| BR-NOT-001 | Neutral Messaging | Pre-decision notifications do not imply a role, badge or approval outcome. |

# Status Models

The platform maintains separate status models for the applicant-facing registration, internal processing, accreditation decision, Digital Entry Pass lifecycle and invitation campaign. Physical Badge stock and issuance are tracked as quantities and events rather than as a personalized badge lifecycle. A status change in one model may trigger a related change but must not collapse the models into one field.

## Applicant-Facing Registration Status

| **Status** | **Meaning** |
| --- | --- |
| Draft | The applicant has started but has not submitted the registration. |
| Submitted | The applicant has confirmed and submitted the current information. |
| Under Review | Authorized users are processing the registration. |
| Additional Information Required | The applicant must complete identified fields or documents. |
| Approved | Accreditation has been granted; Digital Entry Pass and physical-badge readiness are managed separately. |
| Not Approved | Accreditation has not been granted. Internal reasons remain restricted. |
| Withdrawn | The applicant requested withdrawal or an authorized user completed the withdrawal flow. |

## Applicant Status Transitions

| **Current Status** | **Action** | **Resulting Status** |
| --- | --- | --- |
| Draft | Applicant submits a valid registration | Submitted |
| Draft | Applicant requests withdrawal | Withdrawn |
| Submitted | Authorized review begins | Under Review |
| Submitted | Applicant requests withdrawal | Withdrawn |
| Under Review | Reviewer requests completion | Additional Information Required |
| Under Review | Authorized user approves | Approved |
| Under Review | Authorized user records not approved | Not Approved |
| Under Review | Applicant requests withdrawal | Withdrawn |
| Additional Information Required | Applicant resubmits requested information | Submitted |
| Additional Information Required | Applicant requests withdrawal | Withdrawn |
| Approved | Applicant withdraws or authorized user cancels | Withdrawn, with the affected accreditation and related Digital Entry Pass revoked |
| Not Approved | Authorized user reopens the application | Under Review |
| Withdrawn | Authorized user reopens when permitted | Under Review |

## Internal Processing Status

| **Status** | **Meaning** |
| --- | --- |
| Pending Assignment | The application is available in the work queue and has no current reviewer or group assignment. |
| Assigned | The application is assigned to a user or work group. |
| Verification Pending | Required automated or manual identity verification is incomplete. |
| Duplicate Review | A possible duplicate requires authorized examination. |
| Review in Progress | An authorized user is actively processing the application. |
| Awaiting Applicant | A targeted additional-information request is outstanding. |
| Qualification Complete | The decision and required internal assignment are complete. |
| Closed | Processing ended through not-approved, withdrawal, cancellation or another approved closure reason. |

Internal processing status is not shown directly to applicants. The platform maps it to the appropriate neutral applicant-facing status.

## Accreditation Decision Status

| **Status** | **Meaning** |
| --- | --- |
| Not Decided | No final accreditation decision exists. |
| Approved | Accreditation is valid subject to current assignments and cancellation state. |
| Not Approved | Accreditation was not granted. |
| Cancelled | A previously valid or pending accreditation was cancelled or withdrawn. |

The decision status preserves history; reopening creates a new decision action rather than deleting the prior decision.

## Digital Entry Pass Status

Digital Entry Pass status applies to one approved context and its credential versions, not to the person as a whole. Different passes held by the same person may therefore have different statuses at the same time.

| **Status** | **Meaning** |
| --- | --- |
| Not Generated | No Digital Entry Pass has been generated for the eligible registration or accreditation context. |
| Ready | A Digital Entry Pass has been generated and is ready for activation or participant retrieval according to policy. |
| Active | The Digital Entry Pass is valid for entry verification according to its context and Access Profile. The person may hold other active passes. |
| Revoked | The Digital Entry Pass was invalidated and cannot be used. |
| Replaced | The Digital Entry Pass was invalidated because a newer credential version became active for the same context. |
| Expired | The Digital Entry Pass validity period ended. |

## Digital Entry Pass Status Transitions

| **Current Status** | **Action** | **Resulting Status** |
| --- | --- | --- |
| Not Generated | Authorized user generates Digital Entry Pass | Ready |
| Ready | Digital Entry Pass is activated according to policy | Active |
| Ready | Authorized user invalidates before activation | Revoked |
| Active | Authorized user revokes | Revoked |
| Active | A replacement credential version for the same context is activated | Replaced |
| Active | Validity period ends | Expired |
| Ready | Validity period ends before activation | Expired |

## Physical Badge Stock and Issuance

Official Physical Badges are generic stock items grouped by Badge Type. The platform tracks planned, produced, available and issued quantities and records each distribution as an immutable Physical Badge Issuance Event. It does not create a personalized QR credential or a separate personalized status lifecycle for each generic badge item.

## Invitation Campaign Status

| **Status** | **Meaning** |
| --- | --- |
| Draft | The campaign is being configured and its public link cannot create registrations. |
| Active | The campaign link may create registrations subject to dates and capacity. |
| Suspended | New registrations are blocked temporarily; existing linked records remain available. |
| Expired | The configured end date has passed. |
| Closed | The campaign was ended intentionally and cannot create new registrations. |

## Invitation Campaign Transitions

| **Current Status** | **Action** | **Resulting Status** |
| --- | --- | --- |
| Draft | Authorized user activates a valid campaign | Active |
| Active | Authorized user suspends | Suspended |
| Suspended | Authorized user resumes within validity | Active |
| Active or Suspended | End date passes | Expired |
| Draft, Active or Suspended | Authorized user closes | Closed |
| Expired | Authorized user extends validity and reactivates when permitted | Active |

# Non-Functional Requirements

The requirements in this section define the quality, resilience and operating characteristics expected from V1. They apply to the public registration portal, operations back office, Security and Entry Portal, APIs and background services unless a requirement states a narrower boundary.

The targets below are the initial acceptance baseline. Before performance testing, the product owner and technical team shall approve a Target Capacity Profile containing forecast volumes for persons, registration records, Badge Type Assignments, Digital Entry Passes, physical-badge stock, concurrent public sessions, operational users, checkpoints, validations per second and notification bursts.

## Performance and Capacity

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| NFR-PERF-001 | Must | At least 95% of ordinary page loads and synchronous user-interface actions shall complete within two seconds under the approved Target Capacity Profile, excluding file transfer and time spent waiting for an external service. |
| NFR-PERF-002 | Must | At least 95% of draft saves, registration submissions, permitted searches and ordinary filtered-list requests shall complete within three seconds under the approved Target Capacity Profile, excluding external-service delay. |
| NFR-PERF-003 | Must | At least 95% of online Digital Entry Pass or identity-reference validations shall return an access result within 1.5 seconds under the approved checkpoint load. |
| NFR-PERF-004 | Must | Release performance testing shall include at least twice the forecast peak concurrent load and transaction rate defined in the approved Target Capacity Profile. |
| NFR-PERF-005 | Must | A slow or unavailable external service shall be subject to a controlled timeout and shall produce a recoverable result without freezing the user interface or creating an unsafe automatic decision. |
| NFR-PERF-006 | Must | Bulk imports, exports, reports and notification jobs shall not prevent registration, administrative review or entry validation from meeting their critical service targets. |
| NFR-PERF-007 | Must | The Target Capacity Profile shall be approved before final performance testing and shall be retained with the release evidence. |

## Availability Backup and Continuity

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| NFR-AVL-001 | Must | The production service shall target 99.5% measured availability outside the configured event-critical window, excluding approved scheduled maintenance. |
| NFR-AVL-002 | Must | The production service shall target 99.9% measured availability during the configured event-critical window. |
| NFR-AVL-003 | Must | Planned maintenance shall occur outside the event-critical window unless an authorized emergency change is required. |
| NFR-AVL-004 | Must | Availability shall be measured separately for public registration, operational administration and entry validation so that failure in one area is not hidden by aggregate reporting. |
| NFR-CON-001 | Must | The Security and Entry Portal shall support a limited degraded mode for approved credential and identity-reference verification when venue connectivity is unavailable or unstable. |
| NFR-CON-002 | Must | Degraded mode shall use only encrypted, time-limited, checkpoint-authorized verification data and shall not download identity documents, professional files or unrestricted participant records. |
| NFR-CON-003 | Must | Events recorded in degraded mode shall synchronize automatically and idempotently when connectivity returns, preserving the original event time, checkpoint and device context. |
| NFR-CON-004 | Must | Critical registration, accreditation, credential, physical-badge issuance and entry records shall have a Recovery Point Objective of no more than 15 minutes. |
| NFR-CON-005 | Must | The production service shall have a Recovery Time Objective of one hour during the event-critical window and four hours outside that window. |
| NFR-CON-006 | Must | Enrolled-device PWA continuity shall be the V1 offline fallback. An Event Edge is optional and shall be deployed only after the approved operational go/no-go decision. |

## Security and Privacy Quality

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| NFR-SEC-001 | Must | Network communication and stored sensitive data shall be protected using approved encryption controls appropriate to the deployment environment. |
| NFR-SEC-002 | Must | Privileged operational accounts shall use multi-factor authentication, and all administrative sessions shall use configurable expiration and revocation controls. |
| NFR-SEC-003 | Must | Roles, groups, record scope and action permissions shall be enforced by backend services for every protected operation. |
| NFR-SEC-004 | Must | Application secrets, provider credentials and signing keys shall not be stored in source code or exposed through client applications, logs or exports. |
| NFR-SEC-005 | Must | Authentication, public registration, invitation use, file upload, search and export operations shall apply configurable abuse and rate controls. |
| NFR-SEC-006 | Must | Security testing shall cover authentication, authorization, file handling, Digital Entry Pass validation, identity lookup, export control and common web-application threats before production release. |
| NFR-PRI-001 | Must | Digital Entry Pass QR values shall contain no readable personal identity information and shall not expose direct internal record identifiers. |
| NFR-PRI-002 | Must | General technical logs shall exclude complete NIN values, passport numbers, OTP values, identity-document content and unrestricted contact information. |
| NFR-PRI-003 | Must | Browser and device storage shall retain only the minimum information required for the current function and shall clear protected temporary data on sign-out, expiry or authorized remote invalidation. |
| NFR-PRI-004 | Must | Restricted fields, files, reports and exports shall remain masked or unavailable unless the current user holds the specific permission and record scope. |

## Reliability and Data Integrity

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| NFR-REL-001 | Must | Retried submissions, decisions, credential actions, physical-badge issuance, notification requests and entry events shall not create unintended duplicate operations. |
| NFR-REL-002 | Must | Duplicate-control mechanisms shall preserve the intentional ability for one person to hold multiple registration records, Badge Type Assignments and Digital Entry Passes in different authorized contexts. |
| NFR-REL-003 | Must | Replacing, revoking or expiring one Digital Entry Pass shall not change another pass held by the same person unless an authorized action explicitly targets both or a person-level security restriction applies. |
| NFR-REL-004 | Must | A multi-step sensitive action shall either complete consistently or return a clear recoverable failure without leaving an unsafe partial state. |
| NFR-REL-005 | Must | Event timestamps used for audit, Digital Entry Pass lifecycle, physical-badge issuance and entry control shall use a consistent trusted time source while preserving the applicable display timezone. |
| NFR-REL-006 | Must | Import, synchronization and integration retries shall retain source identifiers and correlation values sufficient to prevent duplicate downstream records. |

## Localization and Internationalization

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| NFR-L10N-001 | Must | All applicant-facing and operational web interfaces shall be available in English, French and Arabic. |
| NFR-L10N-002 | Must | Arabic interfaces shall provide complete right-to-left layout, navigation, form, table, dialog and validation behavior. |
| NFR-L10N-003 | Must | The platform shall retain a preferred interface and communication language for each user and allow that preference to be changed. |
| NFR-L10N-004 | Must | Transactional email, SMS and applicant-facing document templates shall support approved English, French and Arabic variants with an authorized fallback language. |
| NFR-L10N-005 | Must | User-visible labels, option lists, statuses, validation messages and configurable content shall use managed translation resources rather than source-code-only text. |
| NFR-L10N-006 | Must | Personal names and organization names shall preserve the submitted Arabic and Latin forms when available and shall not be machine-translated automatically. |
| NFR-L10N-007 | Must | Technical identifiers, source files, API field names, audit action codes and developer documentation shall remain in English. |

## Usability Accessibility and Compatibility

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| NFR-UX-001 | Must | Public, operational and entry interfaces shall adapt to supported desktop, tablet and mobile-browser viewport sizes without requiring a native mobile application. |
| NFR-UX-002 | Must | Primary workflows shall be operable by keyboard and shall expose meaningful labels, focus order and status feedback to assistive technologies. |
| NFR-UX-003 | Must | Errors, warnings, access decisions and required fields shall not rely on color alone and shall remain understandable in every supported language. |
| NFR-UX-004 | Must | Registration shall provide clear step progress, preserve valid input after a recoverable error and identify the exact field or step requiring attention. |
| NFR-UX-005 | Must | Applicant-facing content shall remain neutral and shall not reveal or solicit internal Participant Role, Badge Type or Access Profile choices. |
| NFR-COMP-001 | Must | The platform shall support the current and previous major stable versions of the approved desktop and mobile browsers defined in the release support matrix. |
| NFR-COMP-002 | Must | Digital Entry Pass scanning shall support approved web-device cameras and authorized NIN, passport, Registration Reference, manual or external-scanner fallbacks. |
| NFR-COMP-003 | Must | Loss of optional browser capabilities shall not expose protected data or silently bypass a mandatory validation step. |

## Observability and Operational Support

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| NFR-OBS-001 | Must | Operational monitoring shall cover service health, response time, error rate, external integration failure, background queues and entry-validation throughput. |
| NFR-OBS-002 | Must | Critical failures and threshold breaches shall create actionable alerts for the authorized support function. |
| NFR-OBS-003 | Must | Requests and background operations shall use non-sensitive correlation identifiers sufficient to trace a failure across services. |
| NFR-OBS-004 | Must | Monitoring and diagnostic access shall be permission-controlled and shall not provide unrestricted access to participant data. |
| NFR-OBS-005 | Must | Event-critical dashboards shall distinguish registration, accreditation, credential, physical-badge operation, notification and entry-service health. |
| NFR-OBS-006 | Should | Operational metrics shall support trend comparison before, during and after the event-critical window. |

## Configurability and Maintainability

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| NFR-MNT-001 | Must | Authorized users shall configure supported form content, taxonomies, roles, groups, Badge Types, Access Profiles, invitation policies, notification templates and localized labels for fixed workflow statuses without a source-code change. Core status values and transition rules remain controlled application behavior. |
| NFR-MNT-002 | Must | Configuration changes affecting security, registration, accreditation, credentials, Badge Types, access or notifications shall be validated, versioned and auditable. |
| NFR-MNT-003 | Must | A configuration change shall support an authorized rollback or restoration path without deleting its audit history. |
| NFR-MNT-004 | Must | External API contracts shall use explicit versioning and controlled compatibility rules so that the separate mobile application and approved integrations are not broken silently. |
| NFR-MNT-005 | Must | Deployment and recovery procedures shall support rollback to a previously validated release while preserving compatible production data. |
| NFR-MNT-006 | Should | Failure in a non-critical integration or background job shall be isolated so that critical registration, review and entry functions remain available where technically possible. |

> **Target Validation**
>
> The initial targets in this section remain subject to validation against the approved Target Capacity Profile and deployment architecture. Any approved change to a target shall be recorded through document change control and corresponding acceptance evidence; it shall not be changed silently during implementation.

# Data and Privacy Requirements

This section defines how participant data is separated, classified, collected, accessed, shared and removed. The platform shall preserve one reusable person profile while keeping each registration, invitation, accreditation and credential context operationally independent.

## Data Model Boundaries

| **Record** | **Product Responsibility** |
| --- | --- |
| Person Profile | Canonical shared identity and contact information for one natural person |
| Participant Account | One authentication account per Person in V1, with verified communication destinations and language preference |
| Registration Context | Independent application, source, campaign, inviting organization, actual organization and submitted answers |
| Accreditation Decision | Approval or not-approved outcome for one Registration Context |
| Participant Role Assignment | Contextual functional or protocol classification for one approved Registration Context |
| Badge Type Assignment | Contextual generic Badge Type for one approved Registration Context |
| Access Profile Assignment | Contextual zones, gates and time rules for one approved Registration Context |
| Digital Entry Pass | Personalized digital credential, opaque QR or Registration Reference, state and versions for one context |
| Physical Badge Stock | Planned, produced, available and issued quantities of generic badges by Badge Type |
| Physical Badge Issuance Event | Record that a person and approved context received a generic Physical Badge of a specific type |
| Verification Record | Minimum NIN, passport or manual-verification status, reference, time and approved comparison result |
| Supporting Document | Protected file requested for a specific operational purpose or exception |
| Entry Event | Context-specific checkpoint decision, verification method, time and authorized user or device context |
| Security Restriction | Authorized person-level restriction that can override every context linked to that person |
| Audit Event | Protected history of actor, action, affected record, result and approved change detail |

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| DR-MDL-001 | Must | The platform shall maintain one canonical Person Profile where an identity match has been resolved by an authorized process. |
| DR-MDL-002 | Must | One Person Profile has at most one active Participant Account in V1 and may be linked to multiple verified login-enabled email Contact Points, Registration Contexts, Accreditation Decisions, Participant Role Assignments, Badge Type Assignments, Access Profile Assignments and Digital Entry Passes when permitted by the applicable rules. |
| DR-MDL-003 | Must | Registration source, campaign, Inviting Organization, Participant Organization, delegation, submitted answers, decision and assignment shall remain attached to their own Registration Context. |
| DR-MDL-004 | Must | Context-specific information shall not overwrite another context automatically. |
| DR-MDL-005 | Must | A verified shared-identity change shall preserve history, trigger applicable reverification and identify affected Digital Entry Passes or contexts for review. |
| DR-MDL-006 | Must | Withdrawal or cancellation of one Registration Context shall not delete or invalidate unrelated valid contexts automatically. |
| DR-MDL-007 | Must | A person-level security restriction shall remain separate from credential records while applying to all contexts linked to the person. |
| DR-MDL-008 | Must | Potential person matches shall never be merged automatically; the resolution shall be authorized and auditable. |

## Data Classification

| **Class** | **Examples** | **Default Handling** |
| --- | --- | --- |
| Public | Event information and public registration guidance | Publicly available with integrity controls |
| Internal | Registration references, campaign state and work-queue status | Authenticated and scope-controlled access |
| Personal | Name, email, mobile, organization and professional profile | Role, group and record-scope controlled |
| Restricted | NIN, passport data, photograph, identity documents, security restrictions and protected audit content | Masked by default, purpose-bound permission, encryption and strong audit |
| Secret | Signing keys, API credentials and recovery codes | Approved secret-management service, rotation and no application logging |

Security-restricted, audit-protected and temporary-sensitive are handling tags applied in addition to the base class. Temporary data such as imports, exports, device caches and generated files must have an approved expiry and removal rule.

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| DR-CLS-001 | Must | Every stored or exchanged data field shall have an approved purpose, classification and access rule in the field catalog. |
| DR-CLS-002 | Must | Interfaces, APIs, exports, logs and background jobs shall enforce the classification of every field they process. |
| DR-CLS-003 | Must | Restricted data and records carrying security-restricted or audit-protected handling tags shall not be included in general-purpose search results or exports. |
| DR-CLS-004 | Must | Data carrying the temporary-sensitive handling tag shall use a controlled location and an automatic removal rule appropriate to its operational purpose. |
| DR-CLS-005 | Must | A lower-sensitivity record shall reference a protected record without copying its restricted contents unnecessarily. |

## Collection and Document Minimization

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| DR-COL-001 | Must | The platform shall collect only fields included in the approved field catalog and required for a defined registration, accreditation, security, entry or communication purpose. |
| DR-COL-002 | Must | Identity and supporting-document requirements shall be conditional and shall not appear when they are not applicable. |
| DR-COL-003 | Must | Successful NIN verification shall remove the default need to collect an Algerian identity-card copy. |
| DR-COL-004 | Must | A passport identity-page copy or another identity document shall be collected only under an approved rule or targeted additional-information request. |
| DR-COL-005 | Must | The platform shall avoid storing repeated copies of the same protected document across registration contexts unless a distinct approved purpose requires it. |
| DR-COL-006 | Must | Every uploaded document shall retain its request purpose, source context, classification and access rule. |
| DR-COL-007 | Must | File uploads shall apply approved type, size and security validation before the file becomes available to an operational user. |
| DR-COL-008 | Must | Residential address, banking data, marital status and unrelated personal information shall not be collected in V1. |

## Access and Visibility

| **Actor** | **Default Permitted View** | **Excluded by Default** |
| --- | --- | --- |
| Applicant | Own claimed profile, registrations, public statuses and available Digital Entry Passes | Internal notes, restricted security reasons and administrative audit |
| Registration Reviewer | Registration, context, ordinary profile and permitted verification result | Unrequested identity documents and security-restricted details |
| Accreditation Manager | Participant context, decision inputs, assignments and affected credentials | Unrelated contexts outside assigned scope |
| Credential Operator | Name, photograph, organization, assignment and Digital Entry Pass state | Identity documents and internal review notes |
| Badge Distribution Operator | Name, photograph, selected context, Badge Type and issuance history | Identity documents and internal review notes |
| Entry Control User | Name, photograph, selected context, credential state and access outcome | Other contexts, invitations, documents and restricted denial reason |
| External Security User | Minimum checkpoint verification view within account validity and scope | Back-office access, professional documents, notes and bulk exports |
| Access Administrator | Users, roles, groups, permissions and account state | Participant records unless separately authorized |
| Technical Support | Masked diagnostics, correlation values and service state | Unmasked participant data unless separately authorized |
| Inviting Organization | No participant-level access merely because its campaign was used | Other registrations, other organizations and participant documents |
| Auditor or Oversight User | Read-only records explicitly included in assigned oversight scope | Operational modification actions |

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| DR-ACC-001 | Must | Data access shall be granted through roles, groups, record scope and explicit permissions and shall be enforced by backend services. |
| DR-ACC-002 | Must | Holding an invitation link, administering technical settings or administering access shall not create unrestricted participant-data access. |
| DR-ACC-003 | Must | Linking multiple contexts to one person shall not reveal those contexts to an inviting organization, security user or another unauthorized party. |
| DR-ACC-004 | Must | External security users shall see an access outcome without a restricted person-level reason unless a separate permission explicitly permits that detail. |
| DR-ACC-005 | Must | Viewing, downloading or exporting protected identity documents or data carrying the security-restricted handling tag shall be auditable. |
| DR-ACC-006 | Must | Technical-support views shall use masked values by default and shall use non-sensitive correlation identifiers for investigation. |
| DR-ACC-007 | Must | An applicant shall not receive access to a record created on the applicant's behalf until the approved account-claim or identity-linking process succeeds. |
| DR-ACC-008 | Must | A permitted action shall execute directly without a secondary approval workflow while remaining subject to authorization and audit. |

## External Sharing and Integration Boundaries

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| DR-SHR-001 | Must | Every external integration shall use an approved purpose-specific field allowlist and authenticated interface. |
| DR-SHR-002 | Must | The separate mobile application may receive only approved non-sensitive participant, organization or public-profile data through a versioned read-only API. |
| DR-SHR-003 | Must | Public participant-directory or public-profile visibility in the mobile application shall require a separate optional preference and shall not be required for conference registration. |
| DR-SHR-004 | Must | The mobile application shall not receive NIN, passport data, identity documents, internal notes, verification-service detail, restricted security information or administrative audit history. |
| DR-SHR-005 | Must | The NIN verification service shall receive only the minimum values required by its approved contract, and the platform shall retain only the approved minimal result. |
| DR-SHR-006 | Must | Email and SMS providers shall receive only the destination, approved template content and technical delivery metadata required for the message. |
| DR-SHR-007 | Must | A Physical Badge producer shall receive only approved generic Badge Type artwork and quantity instructions and shall not receive participant records, NIN, passport number or identity-document files. |
| DR-SHR-008 | Must | No external application shall connect directly to the registration database. |
| DR-SHR-009 | Must | Bulk sharing or export shall require a distinct permission, approved field template and auditable generation event. |
| DR-SHR-010 | Must | Integration failures and retries shall not expand the approved data set or duplicate downstream records. |

## Retention Removal and Withdrawal

The PRD does not prescribe a separate numeric retention period for each data category. Exact periods will be maintained in an approved operational policy and configured where applicable without changing the product principles below.

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| DR-RET-001 | Must | Data shall be retained only while required for an approved operational, security, audit or applicable organizational obligation. |
| DR-RET-002 | Must | Retention and removal rules shall be configurable by data category and shall be executable only by an authorized function. |
| DR-RET-003 | Must | Protected documents shall be removed when their approved operational purpose and any applicable obligation have ended. |
| DR-RET-004 | Must | Import files, exports, temporary downloads and device caches shall be removed automatically after their approved use or availability window. |
| DR-RET-005 | Must | Withdrawal or cancellation shall stop the affected ordinary processing and access but shall not imply immediate deletion when a valid retention obligation remains. |
| DR-RET-006 | Must | Full removal of a Person Profile shall not invalidate unrelated active contexts and shall occur only when no active context or applicable retention obligation remains. |
| DR-RET-007 | Must | Where full removal is not permitted, the platform shall support restriction, masking or approved anonymization appropriate to the remaining purpose. |
| DR-RET-008 | Must | Backup, archive and recovery handling shall prevent removed data from returning to ordinary active use and shall follow the approved backup lifecycle. |
| DR-RET-009 | Must | Removal, anonymization and retention-policy actions shall be auditable without preserving the removed content unnecessarily. |

## Privacy Notices Preferences and Applicant Control

For this platform, the Ministry of Knowledge Economy, Start-ups and Micro-enterprises is the Data Controller and platform operator. Personal-data processing shall be governed by Algerian Law No. 18-07 on the protection of natural persons with regard to the processing of personal data, as amended and supplemented by Law No. 25-11, together with other applicable approved requirements. Final notices, legal bases, contact details and multilingual wording require legal and data-protection validation before public launch.

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| DR-PRV-001 | Must | Applicant-facing privacy notices shall be available in English, French and Arabic before submission. |
| DR-PRV-002 | Must | Mandatory registration processing, optional marketing communication and optional public-profile visibility shall remain separate choices. |
| DR-PRV-003 | Must | The platform shall record the notice or preference version, time and participant action without treating optional choices as registration requirements. |
| DR-PRV-004 | Must | Applicants shall view their submitted information and use the approved correction, additional-information or withdrawal processes. |
| DR-PRV-005 | Must | A request to remove, restrict or correct protected data shall be traceable and handled by an authorized user according to the approved policy. |
| DR-PRV-006 | Must | A person registered by an authorized user or organization shall be able to claim the applicable record through an approved verification process. |
| DR-PRV-007 | Must | Claiming one context shall not expose another context unless the account and person linkage has been verified and the applicant is permitted to view it. |

## Data Quality and Provenance

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| DR-QLT-001 | Must | Material identity, contact, organization, decision and assignment values shall retain their source and latest authorized update context. |
| DR-QLT-002 | Must | When a record is created on behalf of a participant, the creator and contact person shall remain distinct from the participant. |
| DR-QLT-003 | Must | Verified, applicant-declared, organization-supplied, imported and administratively corrected values shall remain distinguishable. |
| DR-QLT-004 | Must | Identity-verification status shall remain separate from the stored identity value and shall preserve the verification method and time. |
| DR-QLT-005 | Must | A shared identity change shall identify registrations, accreditations, Badge Type Assignments and Digital Entry Passes that may require review without changing their context-specific values automatically. |
| DR-QLT-006 | Must | Imports and integrations shall preserve source references and shall report rejected or ambiguous records without silently discarding them. |
| DR-QLT-007 | Must | Linking records to a canonical Person Profile shall preserve the original registration source, invitation and audit history. |

> **Simple Retention Principle**
>
> The platform provides configurable retention, removal, masking and anonymization capabilities. Exact retention periods are approved and maintained as an operational policy rather than hardcoded into this PRD.

# Integration Requirements

The web platform shall remain operationally complete without the separate mobile application. Every integration uses a controlled boundary, a defined data allowlist and a safe failure path. Detailed payloads, authentication mechanisms and endpoint definitions belong in the Technical Requirements Document and API contract.

## Integration Landscape

| **Integration** | **Criticality** | **Product Boundary** |
| --- | --- | --- |
| Official ASC Website | Required routing | Links to the web platform and may show registration availability; receives no participant records. |
| NIN Verification Service | Required with fallback | Receives minimum approved identity values and returns a minimal verification result. |
| Email Delivery Service | Required | Delivers OTP and transactional messages in English, French or Arabic. |
| SMS Service | Optional | Provides a secondary notification channel and is not required for primary authentication. |
| Physical Badge Producer | Required operational dependency | Receives generic Badge Type artwork and batch quantities before the event, never participant data. |
| Separate Mobile Application | Optional | May consume a limited read-only API and is not required for registration, credential delivery or entry. |

## Official Website Integration

The official website shall direct users to a stable registration-platform URL. It may pass an approved language parameter and may display a platform-provided state such as open, paused or closed. The registration platform remains directly reachable and does not depend on the website runtime for form completion.

## NIN Verification Integration

The Algerian NIN integration supports identity verification during registration or authorized review. It shall not be called automatically at every venue entry. Entry lookup normally compares the supplied NIN with the already approved platform record.

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| IR-NIN-001 | Must | The platform shall transmit only the values required by the approved NIN-service contract. |
| IR-NIN-002 | Must | The platform shall store only the approved minimal result, reference, method and time rather than a complete provider response. |
| IR-NIN-003 | Must | Successful NIN verification shall remove the default requirement to retain an Algerian identity-card copy. |
| IR-NIN-004 | Must | Timeout, outage, unidentified and inconclusive results shall route to manual examination and shall not create automatic approval or rejection. |
| IR-NIN-005 | Must | Requests and responses shall use non-sensitive correlation identifiers in general operational logs. |
| IR-NIN-006 | Must | Entry lookup by NIN shall use the stored approved identity record unless a separately authorized reverification action is required. |

## Messaging Integrations

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| IR-MSG-001 | Must | Email shall support OTP and transactional notifications using reviewed English, French and Arabic templates. |
| IR-MSG-002 | Must | Message requests shall be idempotent and shall support queueing, controlled retry and delivery-outcome recording. |
| IR-MSG-003 | Must | Provider failure shall not lose the underlying registration or administrative action. |
| IR-MSG-004 | Must | Email and SMS providers shall receive only the approved destination, content and necessary delivery metadata. |
| IR-MSG-005 | Should | Optional SMS failure shall not block registration, approval or entry when an approved alternative channel is available. |
| IR-MSG-006 | Must | OTP delivery shall remain logically and operationally separate from ordinary notifications and optional marketing communication. |

## Physical Badge Production Boundary

The Physical Badge producer receives approved print-ready artwork for each Badge Type and the requested batch quantities. Generic badges are manufactured before the event, inspected and delivered as stock. V1 does not require live printer integration at venue entry or transmission of participant data to the producer.

## Optional Mobile Application Boundary

If enabled, the mobile integration shall be read-only, versioned and restricted to an approved non-sensitive allowlist. It shall not write registration, decision, credential, security or entry data; shall not connect directly to the database; and shall not deliver or display the Digital Entry Pass QR in V1. Its availability is not a release dependency for the web platform.

## Common Integration Controls

| **ID** | **Priority** | **Requirement** |
| --- | --- | --- |
| IR-COM-001 | Must | Integrations shall use dedicated service identities and shall not share interactive user credentials. |
| IR-COM-002 | Must | Every outbound and inbound data set shall use an explicit purpose-specific field allowlist. |
| IR-COM-003 | Must | No external system shall receive direct database access. |
| IR-COM-004 | Must | Timeouts, retry limits, idempotency behavior and failure handling shall be defined for every integration. |
| IR-COM-005 | Must | Integration logs shall use non-sensitive correlation identifiers and shall not contain credentials, complete identity values or document content. |
| IR-COM-006 | Must | External API contracts shall be versioned and shall define compatibility and deprecation rules. |
| IR-COM-007 | Must | Secrets and signing keys shall be stored in an approved protected secret-management mechanism and never in source code. |

# Analytics and Success Metrics

Analytics shall support management decisions, registration operations and event-day control without obscuring the difference between people, contexts, credentials and events. Aggregate dashboards should avoid personal data; permission-controlled drill-down and export remain available only where operationally required.

## Metric Vocabulary

| **Metric Entity** | **Counting Rule** |
| --- | --- |
| Unique Persons | Count each resolved Person Profile once regardless of the number of registrations or invitations. |
| Registration Records | Count every independent Registration Context, including multiple valid contexts for one person. |
| Accreditation Records | Count context-specific decisions independently. |
| Badge Type Assignments | Count each authorized type assignment for a context. |
| Digital Entry Passes | Count current passes separately from historical credential versions. |
| Physical Badge Issuance Events | Count every recorded distribution or authorized replacement event. |
| Entry Events | Count checkpoint decisions; unique attendance counts each person once for the approved reporting period. |

## Dashboard Groups

| **Dashboard** | **Minimum Measures** |
| --- | --- |
| Management Overview | Unique persons, registrations, public and internal statuses, decisions, countries, sources, invitation organizations, participant roles and Badge Types. |
| Registration Operations | Review backlog, turnaround time, incomplete and additional-information cases, manual verification workload, duplicate review, document requests and invitation conversion. |
| Credential Readiness | Badge Type Assignments, Digital Entry Pass readiness, generic Physical Badge production, available stock, issuance and shortages by type. |
| Event-Day Operations | Unique attendance, checkpoint throughput, validation time, lookup method, denial or escalation counts, stock distribution and degraded-mode state. |
| Service Health | Registration availability, message delivery, external-service failures, background queues and entry-service health. |

## Success Measures

| **Measure Area** | **Measure** | **Interpretation** |
| --- | --- | --- |
| Registration | Completion rate | Submitted registrations divided by valid registration starts, segmented by source and language. |
| Operations | Review turnaround | Time from submission or applicant response to the next authorized review action. |
| Identity | Manual examination rate | Share of submitted cases routed to manual identity examination. |
| Invitations | Campaign conversion | Submitted registrations associated with a campaign compared with valid starts through that campaign. |
| Credentials | Digital pass readiness | Eligible approved contexts with an active or ready Digital Entry Pass. |
| Physical badges | Stock readiness | Produced and available quantity compared with approved forecast and spare policy by Badge Type. |
| Distribution | Issuance progress | Physical Badge Issuance Events compared with expected approved contexts by type. |
| Attendance | Unique attendance | Distinct people with at least one accepted Entry Event in the reporting period. |
| Entry | Validation time | Time from lookup or scan to displayed access result. |
| Communications | Delivery outcome | Successful, delayed and failed transactional messages by channel and template. |
| Reliability | Critical availability | Separate availability for public registration, operations and entry services. |

No final numeric business target is fixed until expected volumes, review capacity, checkpoint design and badge quantities are approved. Event-day operational metrics should be near real time; ordinary management and historical reports need not use unnecessarily complex real-time processing.

> **No Double Counting**
>
> Multiple registrations, assignments, passes or entry events for the same person must not inflate the Unique Persons or unique-attendance measures.

# Operational Readiness and Support

Operational readiness covers the people, configuration, devices, credentials, physical stock, fallback procedures and support arrangements needed to use the web platform safely before and during the conference.

## Pre-Registration Readiness

Before public opening, the team shall confirm:

- reviewed English, French and Arabic form content, privacy notices and transactional templates;
- configured roles, groups, permissions and temporary-account expiry rules;
- functioning email OTP, transactional email and approved NIN fallback;
- configured open-registration settings and organization invitation campaigns;
- responsive-browser, accessibility and full Arabic right-to-left testing;
- production hosting, monitoring, backups, restore procedure and support contacts.

## Credential and Badge Readiness

Before event operations, the team shall:

- finalize Badge Types and Access Profiles;
- generate and test Digital Entry Pass retrieval, printing and QR or Registration Reference validation;
- produce generic Physical Badges by type before the event and inspect artwork, quality and delivered quantities;
- confirm expected stock and spares by Badge Type;
- test a person with multiple approved contexts and multiple Badge Type Assignments;
- verify that no personalized physical-badge printing is required at venue entry.

## End-to-End Operational Rehearsal

At least one controlled rehearsal shall cover registration, authorized on-site registration and its unavailable-state contingency, identity verification and fallback, review, direct decision, separate role/Badge Type/Access Profile assignment, Digital Entry Pass retrieval, multi-context selection, entry lookup by every approved method, person-level restriction, degraded-mode synchronization, generic Physical Badge issuance and operational reporting.

## Event-Day Readiness

Each checkpoint or operational desk shall have the approved devices, supported cameras or scanners where applicable, limited user accounts, tested connectivity, a backup connection where feasible, power continuity and an escalation contact. The Badge Distribution Desk shall receive counted stock by Badge Type and shall record each issuance; it is not a live printing desk.

## Support Model

| **Severity** | **Examples** | **Response Principle** |
| --- | --- | --- |
| Critical | Core registration unavailable, unsafe access decision, widespread entry failure or confirmed data loss | Immediate incident coordination, containment, fallback activation and continuous work until stabilized. |
| Major | Significant degradation, delayed messages, partial checkpoint issue or material reporting problem | Prioritized investigation with an operational workaround and regular updates. |
| Normal | Individual user issue, low-impact defect or non-critical report adjustment | Managed through the ordinary support queue. |

Support should combine on-site coverage for check-in and badge distribution with remote technical backup. During the event-critical window, non-essential changes are avoided. An authorized technical user may execute an emergency change directly when necessary, and the action, reason and result shall be logged without introducing a secondary application approval chain.

## Post-Event Closure

After the event, the team shall synchronize outstanding degraded-mode events, close invitation campaigns, disable temporary external-security accounts and devices, remove temporary working exports, confirm backups, produce the approved operational reports, record incidents and apply the configured retention policy.

# Risks Assumptions and Dependencies

## Principal Risks and Mitigations

| **Risk** | **Potential Effect** | **Primary Mitigation** |
| --- | --- | --- |
| NIN-service outage | Identity verification cannot complete automatically | Route to manual examination; never infer success or rejection. |
| Email-provider outage | OTP or notifications are delayed | Queue and retry idempotently; provide an authorized support path. |
| Venue-connectivity failure | Online entry lookup is unavailable or slow | Use tested degraded mode and backup connectivity with controlled synchronization. |
| Peak arrival queues | Excessive participant waiting time | Prefer Digital Entry Pass scanning while preserving NIN, passport and Registration Reference fallbacks. |
| Incorrect person linking | Contexts or decisions attach to the wrong person | Never merge automatically; require authorized and audited resolution. |
| Multiple-context confusion | Entry or issuance uses the wrong entitlement | Display and require selection of the exact eligible context. |
| Generic badge transfer | A Physical Badge is presented by another person | Never accept the generic badge alone as secure identity proof. |
| Insufficient physical stock | Approved participants cannot receive the intended Badge Type | Forecast by type, produce early, hold spares and monitor issuance quantities. |
| Incorrect Badge Type Assignment | Wrong physical badge or access expectation | Show context clearly, restrict assignment permission and preserve issuance history. |
| Production delay | Physical badges are unavailable before the event | Approve artwork and batch quantities early, track delivery and inspect stock. |
| Device or camera failure | A checkpoint cannot scan a Digital Entry Pass | Hold spare devices and use authorized identity or reference lookup. |
| Stale degraded-mode data | A recent revocation or restriction is missed | Limit offline validity, show staleness warnings and synchronize frequently. |
| Forwarded invitation link | Registration is falsely assumed to prove affiliation | Treat the link only as source attribution, never as affiliation proof or approval. |
| Unauthorized data access | Sensitive participant information is exposed | Enforce least privilege, MFA, account expiry, audit and export control. |
| Sensitive export handling | Data persists outside platform controls | Use fixed templates, permission, encryption, audit and post-use removal procedures. |
| Translation error | Applicant misunderstands a requirement or decision | Review all English, French and Arabic content before opening. |
| Human action on wrong scope | A cancellation or restriction affects the wrong context | Display target scope and consequence clearly and require an action reason. |
| Mobile dependency | Web release is delayed by the separate mobile project | Keep the web platform complete and treat mobile integration as optional. |

## Assumptions

- Expected volumes, checkpoint counts and peak arrival rates will be confirmed before final load testing.
- Badge Types and Access Profiles will be finalized before generic Physical Badge production.
- Sufficient generic Physical Badges and approved spares will be produced before the event.
- Registration, accreditation, badge-distribution and security staff will be assigned and trained.
- NIN and email-provider test environments will be available before final acceptance.
- Venue connectivity, backup connectivity, power and entry devices will be tested on site.
- Legal text, privacy notices and trilingual content will receive the required organizational approval.
- The separate mobile application is not required for web-platform launch or event entry.

## External Dependencies

The product depends on production hosting, the NIN verification service, email delivery, optional SMS, official-website routing, the Physical Badge producer, venue connectivity, checkpoint devices and the availability of trained registration and security teams. The separate mobile application is an optional consumer rather than a release dependency.

> **Controlled Failure Principle**
>
> Failure of an external dependency must never create an unsafe automatic identity, accreditation or entry decision. The platform uses a controlled fallback, a clear exception state or an authorized manual process.

# Acceptance Criteria and Release Readiness

The release is accepted only when the required functional, security, performance, resilience and operational evidence demonstrates that the complete web process can operate safely. Acceptance applies to the delivered configuration and environment, not only to isolated software functions.

## Functional Acceptance

The acceptance suite shall demonstrate that:

- the universal registration form operates in English, French and Arabic with complete Arabic right-to-left behavior and contains no applicant selection of Participant Role, Badge Type or Access Profile;
- open registration, organization invitations, Inviting Organization separation, Participant Organization and multiple contexts for one person operate correctly;
- NIN, passport, masking, reduced-document and manual-examination flows behave as specified;
- authorized users can review, request information, decide and execute permitted actions directly under role and group permissions;
- one person can hold multiple context-specific Badge Type Assignments and Digital Entry Passes;
- a Digital Entry Pass can be generated, viewed, downloaded, printed, validated, revoked and replaced without readable personal data in the QR payload;
- official Physical Badges are generic by Badge Type, produced before the event and contain no name, photograph or personalized QR code;
- physical-badge stock and issuance are recorded independently from Digital Entry Pass state;
- entry succeeds through approved Digital Entry Pass, NIN, passport number, Registration Reference and manual-lookup flows with exact context selection;
- a generic Physical Badge alone cannot establish identity at a controlled checkpoint;
- a person-level security restriction blocks every linked context and the entry interface exposes only minimum information;
- degraded-mode entry events synchronize without unsafe duplication; and
- reports distinguish every entity in the approved metric vocabulary.

## Security and Privacy Gate

Release requires evidence of least-privilege role enforcement, multi-factor authentication for sensitive operational accounts, restricted external-security views, audited sensitive actions, protected exports and temporary files, and absence of NIN, passport values or direct personal identifiers from QR payloads and general logs. Invitation use must not grant an organization participant-level access. No unresolved critical security vulnerability may remain.

## Performance and Resilience Gate

The system shall meet the Section 12 targets under the approved Target Capacity Profile and a test load of at least twice the agreed forecast where that target applies. The evidence shall cover registration, entry response time, backup and restore, NIN outage, email and optional SMS failure, mobile-application unavailability, weak venue connectivity, degraded-mode synchronization and idempotent retry behavior.

## Operational Readiness Gate

Release requires a completed end-to-end rehearsal, confirmed checkpoint and distribution-desk devices, named operational accounts, trained users, support contacts, runbooks, generic Physical Badge production and counted spares, issuance testing, temporary-account controls and a successful backup restoration test.

## Release Blockers

The following conditions block release:

- failure of a critical Must requirement;
- an unresolved critical security defect or unrestricted sensitive-data path;
- confirmed data loss or absence of a successful restore test;
- failure of core registration, decision, credential or entry flows;
- failure of the end-to-end operational rehearsal;
- no safe fallback for a required external service; or
- evidence that the generic Physical Badge can be treated as sole secure identity proof.

Optional SMS, optional mobile integration, future single sign-on, direct printer integration and Could-priority requirements do not block release. A Should-priority requirement may remain open only when an authorized owner records a safe operational workaround and the residual risk is non-critical.

## Required Acceptance Evidence

The release record shall include functional results, security-test results, performance results, backup and restore evidence, rehearsal results, the final role and group matrix, checkpoint and device inventory, configuration snapshot, open issues and workarounds, and the recorded release decision.

## Release Decision

A single Authorized Release Owner records the Go or No-Go decision after reviewing the required evidence. This is a governance record, not a multi-step application approval workflow. Critical blockers cannot be waived; a safe non-critical workaround may be accepted and documented by the authorized owner.

> **Final Product Baseline**
>
> Sections 1 through 18 form the complete PRD baseline for the ASC 2026 Registration Platform. Detailed screen behavior, technical architecture, schema, API payloads and delivery sequencing are defined in the subsequent project documents listed in Section 1.
