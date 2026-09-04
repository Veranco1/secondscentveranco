# Authenticity & Anti-Counterfeit — technisch ontwerp

Status: ontwerp + gefaseerde implementatie, zie `README.md` voor wat al
draait en getest is. Dit document is de tegenhanger van
`docs/ARCHITECTURE.md` (dat de marktplaats/betaalarchitectuur beschrijft)
voor het authenticiteitssysteem.

## Leidend principe

**Geen enkel automatisch signaal — AI/beeldherkenning, batchcode, of door
de verkoper aangeleverde informatie — bewijst ooit alleen dat een parfum
100% echt is.** Automatische systemen doen risicoinschatting en
inconsistentiedetectie. Een badge die "geverifieerd" claimt komt alleen
tot stand via het vaste, hieronder gedefinieerde verificatieproces (§ 5),
nooit automatisch bij een lage risicoscore alleen.

Dit principe is verwerkt als een harde regel in de code, niet als
richtlijn: `verification_status` en `SecondScent Verified` worden op
precies één plek gezet (`app/authenticity/verification.py`), en die
functie kan de status `secondscent_verified` alleen bereiken via een
expliciete admin-goedkeuring die aan de standaard in § 5.3 voldoet —
nooit als uitkomst van de risk engine alleen.

---

## A. Analyse — wat er al staat, wat dit toevoegt

`db/schema.sql` had al een volledig `listings`/`listing_photos`-ontwerp
uit Fase 0, maar de sqlite3-devlaag (`app/db.py`) had tot nu toe alleen
een minimale `listings`-tabel (alleen om tegen af te rekenen — "advertenties
komen later" was destijds expliciet uitgesteld) en er bestond helemaal
geen endpoint om een advertentie of foto aan te maken.

Dit systeem vráágt om advertenties met foto's, dus dit bouwt het minimaal
noodzakelijke stuk van "advertenties" mee: advertentie aanmaken + foto's
uploaden. Wat bewust NIET in scope is (blijft voor de latere
advertentie-fase): bewerken/verwijderen van advertenties, zoeken/filteren,
biedingen, favorieten, de publieke browse-UI. Alleen wat de
authenticiteitsflow nodig heeft.

## B. Architectuur

```
seller                buyer                        admin
  |                     |                             |
  v                     v                             v
POST /listings    POST /orders/<id>/report-counterfeit  GET /admin/listings/review-queue
POST /listings/<id>/photos                              POST /admin/listings/<id>/review
POST /listings/<id>/submit-for-review                   GET /admin/intelligence/*
  |                                                      |
  v                                                      |
listing_photos ---> photo_integrity.analyze() ---+       |
  |                  (hash/EXIF/blur/ELA)         |       |
  v                                                v       |
risk_engine.assess() <---------------------- risk_signals |
  |                                                        |
  v                                                        |
verification.apply_outcome()  --(high risk)--> manual_reviews queue <---+
  |
  v
listings.verification_status (+ cached risk_band, NEVER the raw signals)
```

Elke laag is een los, testbaar Python-module (net als de
`stripe_client.py`-abstractie in het betaalsysteem):

- `app/authenticity/photo_integrity.py` — puur functies op bytes in,
  signalen uit. Gebruikt echte, uitlegbare technieken (géén "black box"
  AI-claim): SHA-256 (exacte duplicaten), difference-hash / dHash
  (perceptuele gelijkenis, ook na compressie/crop), EXIF-metadata waar
  aanwezig, Laplacian-variance (wazigheid), en Error Level Analysis
  (compressie-inconsistentie als zwak manipulatiesignaal). Alles draait
  lokaal met Pillow/NumPy/OpenCV (al aanwezig in deze omgeving) — geen
  externe AI-API, dus ook geen "AI heeft dit geverifieerd"-claim mogelijk.
- `app/authenticity/risk_engine.py` — regelgebaseerd, elke regel een
  losse functie die 0 of meer `risk_signals`-rijen produceert met een
  gewicht. De som bepaalt de band (low/medium/high). De regel-internals
  (gewichten, drempels) worden nooit via een publieke of verkoper-API
  geserialiseerd — alleen `verification_status` en (voor admins) de
  signalen zelf zijn zichtbaar.
- `app/authenticity/verification.py` — de statusmachine + de enige plek
  die de `secondscent_verified`-badge mag toekennen, en die 'm automatisch
  laat vervallen zodra kernvelden of verplichte foto's wijzigen ná
  verificatie.

## C. Databasewijzigingen (`db/schema.sql` + `app/db.py`)

**`listings`** (uitgebreid): + `barcode TEXT`, `verification_status TEXT
NOT NULL DEFAULT 'unverified'`, `risk_band TEXT`, `risk_score INTEGER`,
`verification_version INTEGER`, `verified_at TIMESTAMPTZ`. De laatste vier
zijn een **cache** van de laatste rij in `risk_assessments` /
`listing_verifications` — puur voor snelle reads, nooit de bron van
waarheid.

**`listing_photos`** (uitgebreid): + `category TEXT NOT NULL` (12
categorieën, zie § D), `file_ref TEXT NOT NULL` (pad in de privé
evidence-store, nooit een publieke URL), `sha256_hash TEXT NOT NULL`,
`phash TEXT` (perceptuele hash, hex), `width`/`height INTEGER`,
`exif_json TEXT`, `integrity_flags TEXT` (JSON — het resultaat van
`photo_integrity.analyze()`), `seller_entered_code TEXT` (voor de
batchcode/barcode-categorieën: wat de verkoper zélf intypt, zie § D.1).

**Nieuw, append-only** (zelfde patroon als `dispute_evidence` e.a. —
database-trigger die UPDATE/DELETE weigert):
- `risk_signals` — één rij per gedetecteerd signaal, nooit aan
  koper/verkoper getoond.
- `risk_assessments` — één rij per herberekening van de score
  (`score`, `band`, `engine_version`, `signal_ids`, `computed_at`).
- `listing_verifications` — één rij per statuswijziging (`status`,
  `method` [`automated`/`manual`/`system`], `reviewer_id`, `notes`,
  `verification_version`, `created_at`, `superseded_at`) — de volledige
  geschiedenis van een advertentie's verificatiestatus.
- `listing_review_actions` — audit trail van reviewer-acties
  (`approve`/`request_more_evidence`/`reject`/`escalate`), wie, wanneer,
  notities.
- `authenticity_reports` — koppelt een door een koper gemelde
  authenticiteitstwijfel (via de bestaande `disputes`-tabel,
  `reason = 'counterfeit_suspected'`) aan gestructureerde
  vergelijkingsdata met de oorspronkelijke listing-evidence.

**Niet append-only**: `manual_reviews` (de wachtrij zelf — status
verandert wél: pending → in_review → decided; de append-only
`listing_review_actions` ernaast is de onveranderlijke audit trail van
*hoe* die status veranderde).

Alles hierboven wordt zowel in `db/schema.sql` (Postgres, met dezelfde
`forbid_mutation()`-triggers als de bestaande append-only tabellen) als
in `app/db.py` (sqlite3-devlaag, met dezelfde `RAISE(ABORT, ...)`-triggers)
gebouwd, en tegen een echte lokale Postgres 16-instantie geverifieerd —
zelfde discipline als Fase 0.

## D. De 12 fotocategorieën

| # | categorie (code) | verplicht | NL-label getoond aan verkoper |
|---|---|---|---|
| 1 | `bottle_front` | altijd | Voorkant fles |
| 2 | `bottle_back` | altijd | Achterkant fles |
| 3 | `bottle_bottom` | altijd | Onderkant fles |
| 4 | `nozzle` | altijd | Verstuiver / nozzle |
| 5 | `cap` | altijd | Dop |
| 6 | `box_front` | alleen als `box_included` | Doos — voorkant |
| 7 | `box_back` | alleen als `box_included` | Doos — achterkant |
| 8 | `box_bottom` | alleen als `box_included` | Doos — onderkant |
| 9 | `batch_code_bottle` | altijd | Batchcode op de fles |
| 10 | `batch_code_packaging` | alleen als `box_included` | Batchcode op de verpakking |
| 11 | `barcode` | optioneel | Barcode (indien aanwezig) |
| 12 | `proof_of_purchase` | optioneel | Aankoopbewijs (optioneel) |

Bij elke upload toont de API dezelfde vaste instructietekst mee (§ D.2) —
dit is presentatie, geen afdwinging: er is geen betrouwbare, uitlegbare
manier om "voldoende licht" of "geen filter" hard te controleren zonder
zelf onbetrouwbare AI-claims te doen, dus dat blijft begeleiding aan de
verkoper. Wat wél hard gecontroleerd wordt: minimaal-resolutie, en de
wazigheids-score uit `photo_integrity.analyze()` (§ E) — een té wazige
foto wordt geweigerd met een duidelijke reden, niet stilzwijgend
geaccepteerd.

### D.1 Batchcodes: geen bewijs, één signaal van velen

Bij de categorieën `batch_code_bottle`, `batch_code_packaging` en
`barcode` typt de verkoper ook de code zélf over (`seller_entered_code`).
Reden: dit systeem doet geen OCR-gebaseerde automatische lezing (dat zou
een ML-claim zijn die we niet kunnen onderbouwen in deze omgeving) — de
foto is het bewijsstuk, de getypte waarde is wat de risk engine
vergelijkt (bottle-code vs. packaging-code moeten voor een authentiek
setje overeenkomen; een fake matched-up setje is voor een fraudeur
lastiger consistent te vervalsen, maar **een kloppende combinatie bewijst
niets** — het is precies één signaal tussen vele, nooit doorslaggevend).

### D.2 Uploadinstructies (getoond bij elke categorie)

> Zorg voor voldoende licht, een scherpe foto (niet wazig), gebruik geen
> filters of bewerkingen, zorg dat tekst goed leesbaar is, en zorg dat het
> hele object in beeld is.

## E. Risk engine — signalen en scoring

De risk engine draait bij: advertentie ingediend voor review, elke nieuwe
foto-upload, en elke wijziging van een kernveld. Elke regel hieronder is
een losse, testbare functie in `app/authenticity/risk_engine.py` die 0+
`risk_signals` teruggeeft; de som van gewichten bepaalt de band.

| signaal | trigger | gewicht* |
|---|---|---|
| `missing_required_photo` | verplichte categorie ontbreekt bij indienen | 15 |
| `batch_code_mismatch` | getypte fles-code ≠ getypte verpakkings-code | 20 |
| `box_claim_inconsistent` | `box_included=true` maar geen doos-foto's, of vice versa | 15 |
| `photo_reuse_cross_account` | (bijna-)identieke foto al gebruikt door ándere verkoper | 40 |
| `photo_reuse_internal_excessive` | dezelfde foto in ≥3 "verschillende" listings van dezelfde verkoper | 20 |
| `price_far_below_reference` | vraagprijs < 40% van een bekende referentieprijs | 25 |
| `new_account_high_value` | account < 14 dagen oud én prijs > €200 | 15 |
| `unusual_listing_velocity` | ≥5 nieuwe listings van hetzelfde merk/parfum door dezelfde verkoper binnen 48u | 20 |
| `prior_counterfeit_report` | verkoper heeft eerdere bevestigde/lopende counterfeit-melding | 35 |
| `low_quality_evidence_photo` | wazigheids-score onder drempel op een verplichte foto | 10 |
| `possible_manipulation_signal` | ELA-signaal boven drempel (zwak, hoge fout-positieve kans) | 10 |

\* gewichten en drempels staan als constanten bovenaan `risk_engine.py`
(niet hardcoded verspreid) zodat ze net zo makkelijk aan te passen zijn
als de fee-structuur — een latere fase kan ze net als `fee_rules`
admin-configureerbaar maken zonder de regelfuncties zelf te wijzigen.

**Band-drempels**: score 0–19 → *low*, 20–49 → *medium*, 50+ → *high*.

**Bewust NIET gebouwd (en waarom)**: het spec noemt "meerdere accounts
met dezelfde signalen waar dit rechtmatig en privacyvriendelijk kan
worden vastgesteld". Dit systeem clustert alléén op iets dat al
legitiem als bewijsmateriaal is opgeslagen — gedeelde foto-hashes tussen
schijnbaar ongerelateerde accounts (`photo_reuse_cross_account` hierboven
*is* in feite die clustering). Er wordt bewust **geen** IP-/device-
fingerprinting toegevoegd om accounts te koppelen: die data wordt nergens
anders in dit systeem verzameld, en dat verzamelen introduceren puur voor
deze feature zou een nieuwe, invasieve trackinglaag zijn die niet in
verhouding staat en niet "privacyvriendelijk" is zonder een expliciet
juridisch kader (bewaartermijn, rechtsgrond, transparantie) — dat is een
bewuste, gedocumenteerde beperking, geen omissie.

### E.1 Wat de gebruiker ziet vs. wat intern blijft

- Verkoper/koper zien: `verification_status` + de vaste, eerlijke
  uitleg per status (§ F). Nooit een score, nooit welk signaal precies
  afging.
- Admin ziet: alles — de individuele `risk_signals` met hun details,
  in het review-dashboard (§ G).

## F. Statussen en wat ze wérkelijk betekenen

| status | betekenis (getoond aan gebruikers) | wanneer |
|---|---|---|
| `unverified` | "Nog geen enkele controle uitgevoerd." | net aangemaakt, nog niet ingediend |
| `automated_checks_completed` | "Automatische controles zijn uitgevoerd en gaven geen aanleiding tot extra actie. Dit is **geen** garantie van echtheid — er heeft geen menselijke beoordeling plaatsgevonden." | risicoband = low |
| `additional_verification_required` | "We vragen de verkoper om aanvullend bewijs. De advertentie kan zichtbaar zijn terwijl dit loopt." | risicoband = medium |
| `manual_review` | "Deze advertentie wordt handmatig beoordeeld voordat hij zichtbaar wordt." | risicoband = high, of reviewer escaleert |
| `secondscent_verified` | "Beoordeeld door een SecondScent-reviewer volgens onze verificatiestandaard (zie § 5.3). Dit vermindert het risico aanzienlijk maar is **geen 100% garantie van echtheid** — SecondScent biedt geen juridische echtheidsgarantie." | expliciete admin-goedkeuring, zie § F.1 |
| `rejected` | "Deze advertentie voldoet niet aan onze voorwaarden en is niet gepubliceerd." | admin `reject`, of ernstig signaal |

**Verboden tekst, letterlijk gehandhaafd in de UI-laag** (§ I): nergens
in dit systeem verschijnt "100% echt gegarandeerd" of een equivalent —
SecondScent heeft geen proces of juridische garantie die zo'n claim kan
dragen. `tests/test_authenticity.py` bevat een grep-achtige test die dit
letterlijk controleert op elke gebruikersgerichte tekst-constante.

### F.1 `secondscent_verified` — de vaste standaard

Een advertentie kan **alleen** naar `secondscent_verified` als **alle**
onderstaande waar zijn op het moment van goedkeuring (afgedwongen in
`verification.apply_outcome()`, niet als suggestie):

1. Alle verplichte foto's aanwezig (§ D), geen enkele met een
   `low_quality_evidence_photo`-signaal.
2. Fles- en verpakkingsbatchcode zijn ingevuld én komen overeen (of er is
   een reviewer-notitie die het ontbreken/verschil expliciet beoordeelt).
3. Geen onopgeloste `risk_signals` met gewicht ≥ 20 zonder reviewer-notitie
   die uitlegt waarom het signaal hier niet van toepassing is.
4. Een admin heeft de zaak expliciet met `approve` afgerond (nooit
   automatisch).

Bij elke goedkeuring wordt vastgelegd: `verification_method` (altijd
`'manual'` voor deze status), `reviewer_id`, `timestamp`, `evidence`
(de foto/­signaal-ids op dat moment), `verification_version` (het
standaard-versienummer hierboven — als de standaard later verzwaart,
blijven oude verificaties gekoppeld aan de versie waaronder ze zijn
afgegeven), en `status`.

### F.2 Automatisch vervallen

Verandert na `secondscent_verified` een kernveld (`brand`,
`perfume_name`, `size_ml`, `batch_code`, `box_included`) of wordt een
verplichte foto vervangen/verwijderd, dan zet
`verification.on_listing_changed()` de status **automatisch** terug naar
`manual_review` (nooit stilzwijgend naar `unverified` — het feit dat hij
ooit geverifieerd was, is zelf relevante context voor de reviewer) en
logt een `risk_signal` (`possible_manipulation_signal`-achtig type
`post_verification_change`, gewicht 25). Dit is een harde regel, geen UX
suggestie — er is geen code-pad waarop een advertentie de badge behoudt
na zo'n wijziging.

## G. Manual review dashboard (admin)

`GET /admin/listings/review-queue` — lijst van `manual_reviews` met
status `pending`/`in_review`, gesorteerd op wachttijd.

`GET /admin/listings/<id>/review` — de volledige zaak:
advertentiegegevens, alle foto's (met integrity-vlaggen), verkoper-
geschiedenis (aantal eerdere listings, account-leeftijd, eerdere
afwijzingen), de `risk_signals` met details, batchcodes, aankoopbewijs
indien aangeleverd, vergelijkbare listings (zelfde merk+naam, om patronen
te zien), eerdere counterfeit-meldingen tegen deze verkoper, en een
automatisch gegenereerde risk summary (leesbare samenvatting van de
signalen, gegenereerd uit de regelnamen — geen los AI-genereerde tekst).

`POST /admin/listings/<id>/review` — `{action: approve |
request_more_evidence | reject | escalate, notes}`. Elke actie:
1. schrijft een `listing_review_actions`-rij (audit, wie/wanneer/wat),
2. schrijft een `audit_logs`-rij (consistent met de rest van het platform),
3. past `verification_status` aan via `verification.apply_outcome()`,
4. notificeert de verkoper met de eerlijke, vaste statustekst (§ F).

## H. Counterfeit-melding na aankoop

`POST /orders/<id>/report-counterfeit` — alleen de koper van een
`completed`/`inspection_period`/`shipped`-order. Hergebruikt de bestaande
dispute-machinery (`reason='counterfeit_suspected'`, al aanwezig in de
orderstatusmachine) zodat betaling/refund-afhandeling niet dubbel gebouwd
hoeft te worden, en voegt een `authenticity_reports`-rij toe die het
geschil koppelt aan gestructureerde vergelijkingsdata.

Gevraagd bewijs (evidence-categorieën, aparte set van de listing-categorieën
omdat de koper *het ontvangen product* documenteert, niet de advertentie):
`bottle_photo`, `bottle_bottom_photo`, `nozzle_photo`, `batch_code_photo`,
`box_photo`, `packaging_photo`, `discrepancy_description` (verplicht: wat
wijkt af).

`GET /admin/authenticity-reports/<id>` toont de reviewer een
side-by-side: elke categorie-foto die de koper aanleverde naast de
overeenkomstige categorie-foto uit de oorspronkelijke advertentie (waar
beschikbaar) — puur een gestructureerde weergave, geen automatische
"match/no match"-beslissing (dat zou opnieuw een AI-garantie-claim zijn
die we niet kunnen onderbouwen).

## I. Privacy & beveiliging

- **Bewijsbestanden zijn nooit publiek**: foto's en aankoopbewijzen staan
  in een privé evidence-store (`data/evidence/` lokaal in deze sandbox,
  documented als "vervang door privé object storage — S3 met signed URLs
  of gelijkwaardig — in productie"); ze zijn alleen op te vragen via een
  geauthenticeerde endpoint die controleert dat de aanvrager de
  verkoper/koper van de betreffende zaak is, óf admin.
- **Aankoopbewijs extra beperkt**: alleen toegankelijk voor de eigenaar en
  admins — een aparte trust&safety-rol (los van "is admin") is toekomstig
  werk en wordt hier niet ten onrechte als aanwezig voorgesteld.
- **Automatische redactie van gevoelige info in documenten** (bijv.
  creditcardnummers op een aankoopbewijs) wordt **niet** geïmplementeerd —
  dat vereist betrouwbare OCR + inpainting die deze omgeving niet
  eerlijk kan leveren. In plaats daarvan: expliciete instructie aan de
  koper/verkoper om gevoelige info zelf af te dekken vóór upload, en
  strikte toegangsbeperking als het echte vangnet.
- **Reviewer-acties gelogd**: elke admin-actie op een listing-review of
  authenticity-report komt in `audit_logs` én `listing_review_actions`
  (append-only).
- **Minimalisatie**: het admin intelligence-dashboard (§ J) toont user-id
  + e-mail waar nodig om te handelen, maar geen extra profielvelden, en
  nooit aankoopbewijs-inhoud buiten de losse, geautoriseerde
  evidence-endpoint.

## J. Admin counterfeit intelligence dashboard

Leesbare, geaggregeerde endpoints — geen los account-dossier, wél
patroonherkenning:
- `GET /admin/intelligence/signal-summary` — frequentie per signaaltype,
  laatste 30 dagen.
- `GET /admin/intelligence/flagged-sellers` — verkopers gesorteerd op
  cumulatief signaalgewicht (id + e-mail, geen extra PII).
- `GET /admin/intelligence/photo-reuse-clusters` — groepen listings die
  dezelfde foto-hash delen over verschillende verkopers heen (de
  kernvorm van cross-account patroonherkenning, zie § E).

## K. Gefaseerd implementatieplan

| Fase | Inhoud | Test |
|---|---|---|
| 1 | Schema (Postgres + sqlite mirror) | live Postgres-triggers + constraints |
| 2 | Listing aanmaken + foto-upload (12 categorieën, instructies) | Flask test-client |
| 3 | `photo_integrity.py` (hash/EXIF/blur/ELA) | echte gegenereerde testafbeeldingen |
| 4 | `risk_engine.py` | elke regel losse unit-test + integratie |
| 5 | `verification.py` (statusmachine, badge, verval) | Flask test-client, inclusief het verval-scenario |
| 6 | Manual review dashboard (admin) | Flask test-client, alle 4 acties |
| 7 | Counterfeit-melding na aankoop | Flask test-client, incl. side-by-side |
| 8 | Admin intelligence-dashboard + privacy-checks | Flask test-client + toegangscontrole-tests |
| 9 | Volledige testsuite + README + package | `tests/test_authenticity.py`, alles daadwerkelijk uitgevoerd |

Elke fase levert iets op dat echt draait en getest is voordat de
volgende begint — zelfde discipline als het Buyer Protection-systeem.
