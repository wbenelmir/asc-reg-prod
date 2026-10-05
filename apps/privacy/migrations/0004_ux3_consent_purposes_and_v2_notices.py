"""UX-3 (M22, decision D-11): complete draft notices and two consent purposes.

Adds, without touching any existing row:

* consent purposes `PERSONAL_DATA_PROCESSING` (the explicit processing consent
  given at submission) and `SENSITIVE_ACCOMMODATION_DATA` (asked only when
  accommodation support details are given);
* version `v2-draft` of the Privacy Notice and the Registration Terms in
  English, French and Arabic, published so the registration flow shows them.

The texts are complete drafts, not approved legal copy. Every institutional
fact that is not confirmed (C-09: controller address, data-protection
contact, ANPDP declaration or authorization reference, recipients,
transfers, retention periods) appears as an explicit marker
(`[TO BE CONFIRMED: …]`, `[À CONFIRMER : …]`, `[يُستكمل: …]`), and system check
`privacy.W001` reports those markers as release blockers. No approval by the
ANPDP or by a legal function is claimed.

Verified sources (accessed 2026-09-29), recorded in
the UX-3 review record: the ANPDP-hosted consolidated text of
Law No. 18-07 of 10 June 2018 as amended and supplemented by Law No. 25-11 of
24 July 2025 (title page), and the ANPDP FAQ (processing sensitive data, or
transfers abroad, requires a prior authorization rather than a declaration).

The published version each one replaces (normally `v1-draft`) is RETIRED
with `effective_until` set, so there is one PUBLISHED version per document and
language. Its content and every acceptance or consent record that references
it stay unchanged.

UX-C2 (UX-F04, owner decision R1, edited while the migration was unapplied on
every known persistent database): the forward refuses target collisions and
competing published versions before any change. The reverse is a guarded,
intentionally partial reversal that refuses atomically whenever it cannot
establish exact safety, and it always keeps the consent purposes. See
`forward` and `backward`.
"""

from __future__ import annotations

import hashlib

from django.db import migrations
from django.utils import timezone

VERSION_LABEL = "v2-draft"

PRIVACY_EN = """[DRAFT v2 -- complete draft pending legal validation. Not approved by the ANPDP.]

1. Who is responsible
The data controller is the Ministry of Knowledge Economy, Startups and Micro-Enterprises (Algeria), organizer of the African Startup Conference 2026. Address: [TO BE CONFIRMED: official postal address of the controller]. Data-protection contact: [TO BE CONFIRMED: e-mail or postal contact for data-protection questions and requests].

2. Legal framework
Your personal data is processed under Law No. 18-07 of 10 June 2018 on the protection of natural persons in the processing of personal data, as amended and supplemented by Law No. 25-11 of 24 July 2025. Declaration or authorization of this processing with the National Authority for the Protection of Personal Data (ANPDP): [TO BE CONFIRMED: ANPDP receipt or authorization reference].

3. What we collect
- Identity: given names and family name as written in your identity document, date of birth, nationality and country of residence.
- Identity document: your national identity number or your passport number, issuing country and expiry date. These numbers are stored encrypted. A photo of the passport identity page is requested only when the event requires it.
- Contact: your verified e-mail address and your mobile number.
- Professional information: organization, organization type, job title, department, sector, country, website, professional profile link, a short biography and an optional profile photograph.
- Interests and objectives for the event.
- Accessibility support, only if you choose to tell us (see section 5).
- Participation records: your registration status, the passes issued to you and your entry at checkpoints.
- Security records: sign-in codes are never stored in readable form; the network address of a request is used, in keyed form, to limit abuse; important actions are recorded in an audit trail.

4. Why we use it
- To receive and process your registration and to verify your identity where required.
- To let authorized organizers decide on your participation and issue your passes and badges. You never choose a role, badge type or access level yourself.
- To control access and keep the event safe.
- To contact you about your registration and the event.
- To arrange accessibility support you asked for.
- To send optional news about future editions, only if you agreed to it separately.
- To meet legal obligations and keep a record of important actions.
No decision about you is taken by automated means alone.

5. Accessibility support
Telling us about support needs is voluntary and has no effect on your registration. We ask only for the practical support that would help you, never for a diagnosis or a medical certificate. This information is processed only with your separate, explicit consent, is visible only to the support-coordination staff, is kept apart from registration decisions, and never appears on passes, QR codes, exports or e-mails. You can withdraw that consent at any time from your workspace; the support details are then deleted, unless a legal hold requires keeping them.

6. Legal basis
Processing for your registration and participation rests on the consent you give when you submit it, and on the organizer's duties for a public event. Accessibility information rests on your separate explicit consent. Optional news rests on a further, separate consent. [TO BE CONFIRMED: legal basis for each purpose, as assessed by the controller's legal function.]

7. Who receives it
Authorized staff of the organizer, within their role. [TO BE CONFIRMED: other recipients, for example security services, badge producer, e-mail provider and hosting provider.]

8. Transfers outside Algeria
[TO BE CONFIRMED: whether any data is stored or processed outside Algeria, and the legal safeguards and ANPDP authorization applied.]

9. How long we keep it
[TO BE CONFIRMED: retention period for each category of data.] Data subject to a legal hold is kept until the hold is released.

10. Your rights
You may ask for access to your data, for its correction, and object to its processing or ask for its deletion, as provided by law. You may withdraw any consent at any time; withdrawal does not affect processing already carried out, and processing that does not rest on that consent continues. Send requests to the data-protection contact above. You may also contact the ANPDP.

11. Security
Identity document numbers and accessibility notes are encrypted. Access is limited by role and scope and is recorded.

12. Changes
Each version of this notice is dated and kept. The version you acknowledged is recorded with your registration.
"""

PRIVACY_FR = """[BROUILLON v2 -- projet complet en attente de validation juridique. Non approuvé par l'ANPDP.]

1. Responsable du traitement
Le responsable du traitement est le Ministère de l'Économie de la Connaissance, des Start-ups et des Micro-entreprises (Algérie), organisateur de l'African Startup Conference 2026. [À CONFIRMER : dénomination officielle en français.] Adresse : [À CONFIRMER : adresse postale officielle du responsable du traitement]. Contact pour la protection des données : [À CONFIRMER : adresse électronique ou postale pour les questions et demandes relatives aux données personnelles].

2. Cadre juridique
Vos données personnelles sont traitées dans le cadre de la loi n° 18-07 du 10 juin 2018 relative à la protection des personnes physiques dans le traitement des données à caractère personnel, modifiée et complétée par la loi n° 25-11 du 24 juillet 2025. Déclaration ou autorisation de ce traitement auprès de l'Autorité nationale de protection des données à caractère personnel (ANPDP) : [À CONFIRMER : référence du récépissé ou de l'autorisation de l'ANPDP].

3. Données collectées
- Identité : prénoms et nom tels qu'ils figurent sur votre pièce d'identité, date de naissance, nationalité et pays de résidence.
- Pièce d'identité : votre numéro d'identification national ou votre numéro de passeport, le pays de délivrance et la date d'expiration. Ces numéros sont conservés chiffrés. Une photo de la page d'identité du passeport n'est demandée que si l'événement l'exige.
- Contact : votre adresse électronique vérifiée et votre numéro de téléphone mobile.
- Informations professionnelles : organisation, type d'organisation, fonction, département, secteur, pays, site web, lien de profil professionnel, courte biographie et photo de profil facultative.
- Centres d'intérêt et objectifs pour l'événement.
- Besoins d'accessibilité, uniquement si vous choisissez de nous les indiquer (voir la section 5).
- Données de participation : statut de votre inscription, laissez-passer délivrés et passages aux points de contrôle.
- Données de sécurité : les codes de connexion ne sont jamais conservés sous forme lisible ; l'adresse réseau d'une requête est utilisée, sous forme chiffrée par clé, pour limiter les abus ; les actions importantes sont consignées dans un journal d'audit.

4. Finalités
- Recevoir et traiter votre inscription et vérifier votre identité lorsque cela est requis.
- Permettre aux organisateurs autorisés de statuer sur votre participation et de délivrer vos laissez-passer et badges. Vous ne choisissez jamais vous-même un rôle, un type de badge ou un niveau d'accès.
- Contrôler les accès et assurer la sécurité de l'événement.
- Vous contacter au sujet de votre inscription et de l'événement.
- Organiser l'aide à l'accessibilité que vous avez demandée.
- Vous envoyer des informations facultatives sur les prochaines éditions, uniquement si vous l'avez accepté séparément.
- Respecter les obligations légales et conserver une trace des actions importantes.
Aucune décision vous concernant n'est prise sur le seul fondement d'un traitement automatisé.

5. Besoins d'accessibilité
Nous indiquer des besoins d'aide est facultatif et sans effet sur votre inscription. Nous ne demandons que l'aide pratique qui vous serait utile, jamais un diagnostic ni un certificat médical. Ces informations ne sont traitées qu'avec votre consentement distinct et explicite, ne sont visibles que par le personnel chargé de la coordination de l'aide, sont tenues à l'écart des décisions d'inscription et n'apparaissent jamais sur les laissez-passer, codes QR, exports ou courriels. Vous pouvez retirer ce consentement à tout moment depuis votre espace ; les détails sont alors supprimés, sauf si une conservation à titre de preuve (legal hold) l'impose.

6. Base juridique
Le traitement nécessaire à votre inscription et à votre participation repose sur le consentement que vous donnez en la soumettant et sur les obligations de l'organisateur d'un événement public. Les informations d'accessibilité reposent sur votre consentement distinct et explicite. Les informations facultatives reposent sur un autre consentement distinct. [À CONFIRMER : base juridique de chaque finalité, selon l'analyse du service juridique du responsable du traitement.]

7. Destinataires
Le personnel autorisé de l'organisateur, dans la limite de ses fonctions. [À CONFIRMER : autres destinataires, par exemple services de sécurité, fabricant de badges, prestataire de courrier électronique et hébergeur.]

8. Transferts hors d'Algérie
[À CONFIRMER : existence éventuelle d'un stockage ou d'un traitement hors d'Algérie, garanties juridiques et autorisation de l'ANPDP.]

9. Durée de conservation
[À CONFIRMER : durée de conservation de chaque catégorie de données.] Les données faisant l'objet d'une conservation à titre de preuve sont gardées jusqu'à sa levée.

10. Vos droits
Vous pouvez demander l'accès à vos données, leur rectification, vous opposer à leur traitement ou demander leur suppression, dans les conditions prévues par la loi. Vous pouvez retirer un consentement à tout moment ; le retrait ne remet pas en cause les traitements déjà effectués, et les traitements qui ne reposent pas sur ce consentement se poursuivent. Adressez vos demandes au contact indiqué ci-dessus. Vous pouvez également saisir l'ANPDP.

11. Sécurité
Les numéros de pièce d'identité et les notes d'accessibilité sont chiffrés. L'accès est limité selon le rôle et le périmètre, et il est journalisé.

12. Modifications
Chaque version de cette notice est datée et conservée. La version dont vous avez pris connaissance est enregistrée avec votre inscription.
"""

PRIVACY_AR = """[مسودة v2 -- مشروع كامل في انتظار المصادقة القانونية. غير معتمد من السلطة الوطنية لحماية المعطيات ذات الطابع الشخصي.]

1. المسؤول عن المعالجة
المسؤول عن المعالجة هو وزارة اقتصاد المعرفة والمؤسسات الناشئة والمؤسسات المصغرة (الجزائر)، الجهة المنظمة لـ المؤتمر الإفريقي للمؤسسات الناشئة ASC 2026. [يُستكمل: التسمية الرسمية بالعربية.] العنوان: [يُستكمل: العنوان البريدي الرسمي للمسؤول عن المعالجة]. جهة الاتصال لحماية المعطيات: [يُستكمل: البريد الإلكتروني أو العنوان البريدي للأسئلة والطلبات المتعلقة بالمعطيات الشخصية].

2. الإطار القانوني
تُعالَج معطياتك الشخصية في إطار القانون رقم 18-07 المؤرخ في 10 يونيو 2018 المتعلق بحماية الأشخاص الطبيعيين في مجال معالجة المعطيات ذات الطابع الشخصي، المعدل والمتمم بالقانون رقم 25-11 المؤرخ في 24 يوليو 2025. التصريح بهذه المعالجة أو الترخيص بها لدى السلطة الوطنية لحماية المعطيات ذات الطابع الشخصي: [يُستكمل: مرجع وصل التصريح أو الترخيص].

3. المعطيات التي نجمعها
- الهوية: الاسم واللقب كما يظهران في وثيقة هويتك، وتاريخ الميلاد، والجنسية، وبلد الإقامة.
- وثيقة الهوية: رقم التعريف الوطني أو رقم جواز السفر وبلد الإصدار وتاريخ الانتهاء. تُحفظ هذه الأرقام مشفّرة. لا تُطلب صورة صفحة الهوية في جواز السفر إلا إذا اقتضت الفعالية ذلك.
- الاتصال: بريدك الإلكتروني الذي تم التحقق منه ورقم هاتفك المحمول.
- المعلومات المهنية: الهيئة ونوعها، والوظيفة، والقسم، والقطاع، والبلد، والموقع الإلكتروني، ورابط الملف المهني، ونبذة قصيرة، وصورة شخصية اختيارية.
- اهتماماتك وأهدافك من المشاركة.
- احتياجات دعم إمكانية الوصول، فقط إذا اخترت إخبارنا بها (انظر القسم 5).
- معطيات المشاركة: حالة تسجيلك، وتصاريح الدخول الصادرة لك، وعبورك نقاط المراقبة.
- معطيات الأمن: لا تُحفظ رموز الدخول أبدًا بصيغة مقروءة؛ ويُستخدم عنوان الشبكة لطلبٍ ما بصيغة مشفّرة بمفتاح للحد من إساءة الاستخدام؛ وتُسجَّل الإجراءات المهمة في سجل تدقيق.

4. أغراض الاستخدام
- استلام تسجيلك ومعالجته والتحقق من هويتك عند الاقتضاء.
- تمكين المنظمين المخوّلين من البت في مشاركتك وإصدار تصاريح الدخول والشارات. لا تختار بنفسك أبدًا دورًا أو نوع شارة أو مستوى وصول.
- مراقبة الدخول وضمان أمن الفعالية.
- التواصل معك بشأن تسجيلك والفعالية.
- ترتيب دعم إمكانية الوصول الذي طلبته.
- إرسال أخبار اختيارية عن الدورات القادمة، فقط إذا وافقت على ذلك بشكل منفصل.
- الوفاء بالالتزامات القانونية والاحتفاظ بأثر للإجراءات المهمة.
لا يُتخذ أي قرار بشأنك بالاستناد إلى معالجة آلية وحدها.

5. دعم إمكانية الوصول
إخبارنا باحتياجات الدعم أمر اختياري ولا يؤثر في تسجيلك. نطلب فقط الدعم العملي الذي يساعدك، ولا نطلب أبدًا تشخيصًا أو شهادة طبية. لا تُعالَج هذه المعلومات إلا بموافقتك الصريحة والمنفصلة، ولا يطّلع عليها إلا الموظفون المكلفون بتنسيق الدعم، وتبقى منفصلة عن قرارات التسجيل، ولا تظهر أبدًا على تصاريح الدخول أو رموز QR أو ملفات التصدير أو رسائل البريد الإلكتروني. يمكنك سحب هذه الموافقة في أي وقت من مساحتك؛ فتُحذف تفاصيل الدعم، ما لم يقتضِ حفظٌ لأغراض قانونية الإبقاء عليها.

6. الأساس القانوني
تستند المعالجة اللازمة لتسجيلك ومشاركتك إلى الموافقة التي تقدمها عند إرسال التسجيل، وإلى التزامات منظم فعالية عامة. وتستند معلومات دعم إمكانية الوصول إلى موافقتك الصريحة والمنفصلة. وتستند الأخبار الاختيارية إلى موافقة منفصلة أخرى. [يُستكمل: الأساس القانوني لكل غرض وفق تقييم المصلحة القانونية للمسؤول عن المعالجة.]

7. المستفيدون من المعطيات
الموظفون المخوّلون لدى الجهة المنظمة، في حدود مهامهم. [يُستكمل: المستفيدون الآخرون، مثل مصالح الأمن ومنتج الشارات ومزوّد البريد الإلكتروني ومزوّد الاستضافة.]

8. النقل خارج الجزائر
[يُستكمل: ما إذا كانت أي معطيات تُخزَّن أو تُعالَج خارج الجزائر، والضمانات القانونية وترخيص السلطة الوطنية المطبّقة.]

9. مدة الحفظ
[يُستكمل: مدة حفظ كل فئة من المعطيات.] تُحفظ المعطيات الخاضعة لحفظ لأغراض قانونية إلى حين رفعه.

10. حقوقك
يمكنك طلب الاطلاع على معطياتك وتصحيحها، والاعتراض على معالجتها أو طلب حذفها، وفقًا لما ينص عليه القانون. ويمكنك سحب أي موافقة في أي وقت؛ ولا يمس السحب المعالجات التي تمت قبله، وتستمر المعالجات التي لا تستند إلى تلك الموافقة. وجّه طلباتك إلى جهة الاتصال المذكورة أعلاه. ويمكنك أيضًا مراسلة السلطة الوطنية لحماية المعطيات ذات الطابع الشخصي.

11. الأمن
أرقام وثائق الهوية وملاحظات دعم إمكانية الوصول مشفّرة. ويقتصر الوصول على الأدوار والنطاقات المخوّلة، ويُسجَّل.

12. التعديلات
تُؤرَّخ كل نسخة من هذا الإشعار وتُحفظ. وتُسجَّل النسخة التي اطلعت عليها مع تسجيلك.
"""

TERMS_EN = """[DRAFT v2 -- complete draft pending legal validation.]

1. These terms apply to registration for the African Startup Conference 2026 organized by the Ministry of Knowledge Economy, Startups and Micro-Enterprises.
2. You confirm that the information you give is true and that your names and identity document details match your identity document.
3. Registration is the same for everyone. Submitting it does not guarantee participation, a role, a badge type or an access level. Authorized organizers decide on participation and assign passes and badges after registration.
4. You keep one account. You may hold several registrations when you are invited by different organizations; each keeps its own origin.
5. Passes and badges are personal and may not be lent, copied or transferred. Show your pass and, when asked, your identity document at entry.
6. The organizer may open, restrict or close registration channels at any time. Closing never deletes a submitted registration or a saved draft.
7. You may withdraw your registration at any time from your workspace.
8. Respect the event rules, the venue rules and the instructions of the staff. The organizer may refuse or cancel access in case of fraud, false information or a safety risk.
9. The programme, dates and venue may change; registered participants are informed through the platform or by e-mail.
10. Personal data is handled as described in the Privacy Notice.
11. Contact: [TO BE CONFIRMED: organizer contact for registration questions]. Applicable law and competent jurisdiction: [TO BE CONFIRMED: as determined by the controller's legal function].
"""

TERMS_FR = """[BROUILLON v2 -- projet complet en attente de validation juridique.]

1. Les présentes conditions s'appliquent à l'inscription à l'African Startup Conference 2026 organisée par le Ministère de l'Économie de la Connaissance, des Start-ups et des Micro-entreprises.
2. Vous confirmez que les informations fournies sont exactes et que vos noms et les données de votre pièce d'identité correspondent à celle-ci.
3. L'inscription est la même pour tous. Sa soumission ne garantit ni la participation, ni un rôle, ni un type de badge, ni un niveau d'accès. Les organisateurs autorisés statuent sur la participation et attribuent les laissez-passer et badges après l'inscription.
4. Vous disposez d'un seul compte. Vous pouvez avoir plusieurs inscriptions si vous êtes invité par différentes organisations ; chacune conserve son origine.
5. Les laissez-passer et badges sont personnels et ne peuvent être ni prêtés, ni copiés, ni cédés. Présentez votre laissez-passer et, sur demande, votre pièce d'identité à l'entrée.
6. L'organisateur peut ouvrir, restreindre ou fermer les canaux d'inscription à tout moment. La fermeture ne supprime jamais une inscription soumise ni un brouillon enregistré.
7. Vous pouvez retirer votre inscription à tout moment depuis votre espace.
8. Respectez le règlement de l'événement, celui du lieu et les consignes du personnel. L'organisateur peut refuser ou annuler l'accès en cas de fraude, d'informations fausses ou de risque pour la sécurité.
9. Le programme, les dates et le lieu peuvent changer ; les participants inscrits en sont informés via la plateforme ou par courriel.
10. Les données personnelles sont traitées comme décrit dans la notice de confidentialité.
11. Contact : [À CONFIRMER : contact de l'organisateur pour les questions d'inscription]. Droit applicable et juridiction compétente : [À CONFIRMER : selon le service juridique du responsable du traitement].
"""

TERMS_AR = """[مسودة v2 -- مشروع كامل في انتظار المصادقة القانونية.]

1. تسري هذه الشروط على التسجيل في المؤتمر الإفريقي للمؤسسات الناشئة ASC 2026 الذي تنظمه وزارة اقتصاد المعرفة والمؤسسات الناشئة والمؤسسات المصغرة.
2. تؤكد أن المعلومات التي تقدمها صحيحة وأن اسمك ومعطيات وثيقة هويتك مطابقة لها.
3. التسجيل واحد للجميع. ولا يضمن إرساله المشاركة أو دورًا أو نوع شارة أو مستوى وصول. يبت المنظمون المخوّلون في المشاركة ويُسندون تصاريح الدخول والشارات بعد التسجيل.
4. لديك حساب واحد. ويمكن أن تكون لك عدة تسجيلات إذا دعتك هيئات مختلفة؛ ويحتفظ كل تسجيل بمصدره.
5. تصاريح الدخول والشارات شخصية ولا يجوز إعارتها أو نسخها أو التنازل عنها. قدّم تصريحك، وعند الطلب وثيقة هويتك، عند الدخول.
6. يجوز للمنظم فتح قنوات التسجيل أو تقييدها أو إغلاقها في أي وقت. ولا يحذف الإغلاق أبدًا تسجيلًا مُرسلًا أو مسودة محفوظة.
7. يمكنك سحب تسجيلك في أي وقت من مساحتك.
8. احترم نظام الفعالية ونظام المكان وتعليمات الموظفين. ويجوز للمنظم رفض الدخول أو إلغاؤه في حال الاحتيال أو تقديم معلومات كاذبة أو وجود خطر على السلامة.
9. قد يتغير البرنامج أو المواعيد أو المكان؛ ويُبلَّغ المشاركون المسجلون عبر المنصة أو بالبريد الإلكتروني.
10. تُعالَج المعطيات الشخصية كما هو موضح في إشعار الخصوصية.
11. جهة الاتصال: [يُستكمل: جهة اتصال المنظم لأسئلة التسجيل]. القانون الواجب التطبيق والجهة القضائية المختصة: [يُستكمل: وفق المصلحة القانونية للمسؤول عن المعالجة].
"""

CONTENT = {
    ("PRIVACY_NOTICE", "en"): PRIVACY_EN,
    ("PRIVACY_NOTICE", "fr"): PRIVACY_FR,
    ("PRIVACY_NOTICE", "ar"): PRIVACY_AR,
    ("TERMS", "en"): TERMS_EN,
    ("TERMS", "fr"): TERMS_FR,
    ("TERMS", "ar"): TERMS_AR,
}

PURPOSES = (
    (
        "PERSONAL_DATA_PROCESSING",
        "Processing of personal data for registration and participation",
        "Traitement des données personnelles pour l'inscription et la participation",
        "معالجة المعطيات الشخصية لأغراض التسجيل والمشاركة",
    ),
    (
        "SENSITIVE_ACCOMMODATION_DATA",
        "Processing of accessibility support information",
        "Traitement des informations relatives à l'aide à l'accessibilité",
        "معالجة معلومات دعم إمكانية الوصول",
    ),
)


class MigrationRefused(RuntimeError):
    """A guarded step refused to run; the migration transaction rolls back."""


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def forward(apps, schema_editor):
    """Guarded forward (UX-C2, UX-F04 option R1).

    Phase 1 checks every document and language before anything changes:

    * A `v2-draft` version that already exists is a collision. Its ownership
      cannot be established, so the migration refuses instead of adopting it.
    * More than one PUBLISHED version is ambiguous, so it refuses too.

    Phase 2 then retires the single published predecessor, if any, and
    creates the v2 version in the same step, from one `now`. The
    predecessor's `effective_until` is the v2 `effective_from`: that pairing
    is what the reverse relies on, together with the collision guard.
    """
    LegalDocument = apps.get_model("privacy", "LegalDocument")
    LegalDocumentVersion = apps.get_model("privacy", "LegalDocumentVersion")
    ConsentPurpose = apps.get_model("privacy", "ConsentPurpose")
    now = timezone.now()
    plan = []
    for (code, language), content in CONTENT.items():
        document, _ = LegalDocument.objects.get_or_create(
            code=code, defaults={"document_type": code}
        )
        versions = LegalDocumentVersion.objects.filter(legal_document=document, language=language)
        if versions.filter(version_label=VERSION_LABEL).exists():
            raise MigrationRefused(
                f"privacy.0004 refused: {code}/{language} already has a {VERSION_LABEL} "
                "version whose ownership cannot be established."
            )
        published = list(versions.filter(status="PUBLISHED"))
        if len(published) > 1:
            raise MigrationRefused(
                f"privacy.0004 refused: {code}/{language} has several PUBLISHED versions."
            )
        plan.append((document, language, content, published[0] if published else None))
    for document, language, content, predecessor in plan:
        if predecessor is not None:
            # Status and end date only; the content and every acceptance or
            # consent record that references it stay unchanged.
            LegalDocumentVersion.objects.filter(pk=predecessor.pk).update(
                status="RETIRED", effective_until=now
            )
        LegalDocumentVersion.objects.create(
            legal_document=document,
            language=language,
            version_label=VERSION_LABEL,
            content=content,
            content_hash=_content_hash(content),
            effective_from=now,
            status="PUBLISHED",
        )
    for code, name, name_fr, name_ar in PURPOSES:
        ConsentPurpose.objects.get_or_create(
            code=code, defaults={"name": name, "name_fr": name_fr, "name_ar": name_ar}
        )


def backward(apps, schema_editor):
    """Guarded fail-safe reverse (UX-C2, UX-F04 option R1): an intentionally
    partial reversal.

    For each document and language, the reverse removes the migration's own v2
    version only when every one of these holds:

    * the version is still exactly as created: the actual content and the
      stored hash both equal the migration's text, it is PUBLISHED, and it has
      no end date;
    * no acceptance or consent record references it;
    * no other version is PUBLISHED for that document and language;
    * the predecessor to restore is unambiguous: exactly one RETIRED version
      whose `effective_until` equals the v2 `effective_from` (the pairing the
      guarded forward wrote). If there is no such version and no retired
      version at all, the forward retired nothing, so nothing is restored.

    If any document or language fails a check, the whole reverse refuses
    BEFORE any change, and the migration transaction rolls back: no partial
    deletion and never two published versions. Unrelated documents, other
    labels and manually altered rows are never touched.

    The two consent purposes are always KEPT. `get_or_create` may have found
    pre-existing rows, and creation ownership cannot be established without a
    provenance record. Recovery from a refused reverse is by roll-forward.
    """
    LegalDocument = apps.get_model("privacy", "LegalDocument")
    LegalDocumentVersion = apps.get_model("privacy", "LegalDocumentVersion")
    AcceptanceRecord = apps.get_model("privacy", "AcceptanceRecord")
    ConsentRecord = apps.get_model("privacy", "ConsentRecord")
    plan = []
    for (code, language), content in CONTENT.items():
        document = LegalDocument.objects.filter(code=code).first()
        if document is None:
            continue
        versions = LegalDocumentVersion.objects.filter(legal_document=document, language=language)
        target = versions.filter(version_label=VERSION_LABEL).first()
        if target is None:
            continue
        where = f"{code}/{language}"
        expected = _content_hash(content)
        if (
            target.content != content
            or target.content_hash != expected
            or target.status != "PUBLISHED"
            or target.effective_until is not None
        ):
            raise MigrationRefused(f"privacy.0004 reverse refused: {where} v2 was altered.")
        if (
            AcceptanceRecord.objects.filter(legal_document_version=target).exists()
            or ConsentRecord.objects.filter(legal_document_version=target).exists()
        ):
            raise MigrationRefused(f"privacy.0004 reverse refused: {where} v2 is referenced.")
        if versions.filter(status="PUBLISHED").exclude(pk=target.pk).exists():
            raise MigrationRefused(
                f"privacy.0004 reverse refused: {where} has another PUBLISHED version."
            )
        paired = list(versions.filter(status="RETIRED", effective_until=target.effective_from))
        if len(paired) > 1:
            raise MigrationRefused(f"privacy.0004 reverse refused: {where} predecessor ambiguous.")
        if not paired and versions.filter(status="RETIRED").exists():
            raise MigrationRefused(
                f"privacy.0004 reverse refused: {where} predecessor cannot be established."
            )
        plan.append((target.pk, paired[0].pk if paired else None))
    for target_pk, predecessor_pk in plan:
        LegalDocumentVersion.objects.filter(pk=target_pk).delete()
        if predecessor_pk is not None:
            LegalDocumentVersion.objects.filter(pk=predecessor_pk).update(
                status="PUBLISHED", effective_until=None
            )


class Migration(migrations.Migration):
    dependencies = [("privacy", "0003_retentioncategory_legalhold_privacyrequest")]

    operations = [migrations.RunPython(forward, backward)]
