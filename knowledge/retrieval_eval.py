"""Retrieval-quality test set + scorer (Step 26).

A labelled set of realistic queries — transaction phrasings and the kind of
questions an accountant (or a department agent) would actually pose — each
mapped to the rule that *should* surface. ``evaluate()`` runs every query
through the production ``retrieve()`` and reports precision@1, recall@3 and
recall@5, plus MRR, broken down by framework and by language.

Why this matters: the agents (Phase P05+) act on whatever ``retrieve()``
returns. This harness lets us *measure* that the right rule is in the top-5
before we build anything on top of it, and re-measure after the planned
ranking upgrades (Postgres FTS → pgvector).

Coverage: every one of the encoded rules appears at least once; high-traffic
rules (rates, caps, depreciation, the chart classes, withholding) twice. The
queries are deliberately NOT copies of rule titles — they share vocabulary
the way a real query would, not verbatim. A small English block measures the
cross-lingual gap (the corpus is French, so English recall is expected to
lag until pgvector — this quantifies exactly how much).

Each entry: (query, expected_slug, framework, lang).
"""

SYS = "SYSCOHADA-2017"
CGI = "CGI-2025"

TEST_SET = [
    # ---- K01 — SYSCOHADA chart of accounts ----
    ("dans quelle classe enregistrer un emprunt bancaire à long terme", "syscohada-class-1", SYS, "fr"),
    ("compte de capital social, réserves et report à nouveau", "syscohada-class-1", SYS, "fr"),
    ("où classer un terrain, une construction ou une machine", "syscohada-class-2", SYS, "fr"),
    ("classe des immobilisations et de leurs amortissements", "syscohada-class-2", SYS, "fr"),
    ("comptes de stocks de marchandises et de matières premières", "syscohada-class-3", SYS, "fr"),
    ("comptes de tiers clients et fournisseurs", "syscohada-class-4", SYS, "fr"),
    ("comptes de trésorerie banque et caisse", "syscohada-class-5", SYS, "fr"),
    ("comptes de charges des activités ordinaires achats services", "syscohada-class-6", SYS, "fr"),
    ("comptes de produits ventes de biens et services", "syscohada-class-7", SYS, "fr"),
    ("charges et produits hors activités ordinaires HAO", "syscohada-class-8", SYS, "fr"),
    ("comptabilité analytique et engagements hors bilan classe 9", "syscohada-class-9", SYS, "fr"),
    ("distinction comptes de bilan et comptes de gestion situation", "syscohada-balance-vs-result", SYS, "fr"),
    ("terminaison 9 pour les dépréciations et provisions", "syscohada-terminaison-9-depreciation", SYS, "fr"),
    ("parallélisme de codification entre charges et produits", "syscohada-charge-produit-parallelism", SYS, "fr"),

    # ---- K15 — SYSCOHADA evaluation ----
    ("coût d'acquisition d'une immobilisation droits de mutation honoraires", "syscohada-eval-acquisition-cost-immobilisation", SYS, "fr"),
    ("coût d'achat de marchandises net de remises rabais ristournes", "syscohada-eval-acquisition-cost-goods", SYS, "fr"),
    ("conventions d'évaluation coût historique prudence", "syscohada-eval-base-conventions", SYS, "fr"),
    ("incorporer les coûts d'emprunt au coût d'un actif qualifié", "syscohada-eval-borrowing-costs-qualified-asset", SYS, "fr"),
    ("règle d'évaluation de l'approche par composants article 38", "syscohada-eval-component-approach", SYS, "fr"),
    ("valeur actuelle et valeur d'inventaire à la clôture de l'exercice", "syscohada-eval-current-value-at-close", SYS, "fr"),
    ("montant amortissable durée d'utilité et modes d'amortissement", "syscohada-eval-depreciation", SYS, "fr"),
    ("conversion des biens acquis en devises à l'entrée", "syscohada-eval-fx-on-entry", SYS, "fr"),
    ("hypothèse de continuité d'exploitation en cas de liquidation", "syscohada-eval-going-concern", SYS, "fr"),
    ("coût historique selon le mode d'entrée apport échange production", "syscohada-eval-historical-cost-by-mode", SYS, "fr"),
    ("amoindrissement définitif ou non amortissement ou dépréciation", "syscohada-eval-impairment", SYS, "fr"),
    ("évaluation des stocks PEPS ou coût moyen pondéré", "syscohada-eval-inventory-fifo-or-wac", SYS, "fr"),
    ("permanence des méthodes comptables d'un exercice à l'autre", "syscohada-eval-permanence-of-methods", SYS, "fr"),
    ("coût réel de production charges directes et indirectes", "syscohada-eval-production-cost", SYS, "fr"),
    ("provisions pour risques et charges conditions de constitution", "syscohada-eval-provisions", SYS, "fr"),

    # ---- K20 — SYSCOHADA first application ----
    ("compte 475 transitoire de révision 4751 et 4752", "syscohada-fta-account-475-transitional", SYS, "fr"),
    ("compte 475 pour protéger le capital perte de la moitié", "syscohada-fta-capital-protection-475", SYS, "fr"),
    ("première application changement de méthode effet rétrospectif report à nouveau", "syscohada-fta-change-of-method-retrospective", SYS, "fr"),
    ("charges immobilisées apurement à la transition virement 4751", "syscohada-fta-charges-immobilisees", SYS, "fr"),
    ("approche par composants à la transition réallocation des VNC", "syscohada-fta-component-approach-choice", SYS, "fr"),
    ("déclaration explicite et sans réserve de conformité", "syscohada-fta-conformity-declaration", SYS, "fr"),
    ("immeubles de placement réajustement des intitulés 2281 2315", "syscohada-fta-investment-property-rename", SYS, "fr"),
    ("contrats de location en cours à la transition nouveaux contrats", "syscohada-fta-leases-prospective", SYS, "fr"),
    ("objectif des dispositions transitoires comptes supprimés", "syscohada-fta-objective", SYS, "fr"),
    ("bilan d'ouverture en SYSCOHADA révisé reclasser les actifs", "syscohada-fta-opening-balance-sheet", SYS, "fr"),
    ("comptes pro-forma informations comparatives exercices antérieurs", "syscohada-fta-proforma", SYS, "fr"),
    ("engagements de retraite à la transition compte 196 indemnités fin de carrière", "syscohada-fta-retirement-commitments", SYS, "fr"),

    # ---- K02 — SYSCOHADA component approach ----
    ("décomposer un bâtiment en structure et composant ascenseur", "syscohada-component-decomposition-principle", SYS, "fr"),
    ("comptes dédiés structure et composant terminaison 1 et 2", "syscohada-component-account-structure", SYS, "fr"),
    ("coût de démantèlement actif de démantèlement provision 1984", "syscohada-component-dismantling-asset", SYS, "fr"),
    ("désactualisation de la provision pour démantèlement compte 6971", "syscohada-component-dismantling-unwinding", SYS, "fr"),
    ("révision majeure ou inspection traitée comme un composant distinct", "syscohada-component-major-revision", SYS, "fr"),
    ("renouvellement d'un composant immobilisation du nouveau augmentation valeur d'origine", "syscohada-component-replacement-capitalization", SYS, "fr"),
    ("renouvellement d'un composant sortie de l'ancien valeur nette comptable", "syscohada-component-replacement-derecognition", SYS, "fr"),
    ("amortir séparément chaque composant et la structure", "syscohada-component-separate-depreciation", SYS, "fr"),

    # ---- K04 — SYSCOHADA asset impairment ----
    ("test de dépréciation indice de perte de valeur comparer à la VNC", "syscohada-impairment-test-principle", SYS, "fr"),
    ("différence entre une dépréciation d'actif et une provision passif", "syscohada-impairment-vs-provision", SYS, "fr"),
    ("comptabiliser la perte de valeur égale à VNC moins valeur actuelle", "syscohada-impairment-recognition", SYS, "fr"),
    ("réviser le plan d'amortissement après une perte de valeur", "syscohada-impairment-revised-depreciation-plan", SYS, "fr"),
    ("reprise de dépréciation plafonnée à la valeur comptable historique", "syscohada-impairment-reversal-cap", SYS, "fr"),
    ("caractère réversible dotation et reprise de dépréciation 7914", "syscohada-impairment-reversibility-mechanics", SYS, "fr"),
    ("comptes de dotation et de dépréciation des immobilisations 6914 291", "syscohada-impairment-accounts", SYS, "fr"),
    ("dépréciation d'un groupe d'actifs affectée d'abord au fonds commercial", "syscohada-impairment-group-allocation", SYS, "fr"),
    ("la dépréciation du fonds commercial ne peut jamais être reprise", "syscohada-impairment-goodwill-no-reversal", SYS, "fr"),
    ("perte de valeur sur une immobilisation réévaluée écart de réévaluation 1062", "syscohada-impairment-revalued-asset", SYS, "fr"),
    ("dépréciation d'une immobilisation financée par subvention deux méthodes", "syscohada-impairment-subsidized-asset", SYS, "fr"),

    # ---- K10 — CGI corporate income tax (IS) ----
    ("qui est passible de l'impôt sur les sociétés SARL et SA", "cgi-2025-is-taxable-persons", CGI, "fr"),
    ("sociétés exonérées d'impôt sur les sociétés coopératives agricoles", "cgi-2025-is-exemptions", CGI, "fr"),
    ("territorialité bénéfices réalisés au Cameroun établissement permanent", "cgi-2025-is-territoriality", CGI, "fr"),
    ("détermination du bénéfice imposable par la variation de l'actif net", "cgi-2025-is-taxable-profit-base", CGI, "fr"),
    ("conditions générales de déductibilité des charges intérêt de l'entreprise", "cgi-2025-is-deductible-charges-general", CGI, "fr"),
    ("déduction des rémunérations cotisations retraite expatrié 15 pour cent jetons de présence", "cgi-2025-is-remuneration-deductibility", CGI, "fr"),
    ("plafond des frais de siège et d'assistance technique 2,5 pour cent du bénéfice", "cgi-2025-is-headquarters-and-technical-fees-cap", CGI, "fr"),
    ("plafond des redevances brevets et marques versées hors CEMAC", "cgi-2025-is-royalties-cap", CGI, "fr"),
    ("plafond des commissions et courtages 1 pour cent des achats", "cgi-2025-is-commissions-cap", CGI, "fr"),
    ("impôts et amendes l'IS et l'IRPP ne sont pas déductibles", "cgi-2025-is-taxes-fines-deductibility", CGI, "fr"),
    ("déduction des primes d'assurance auto-assurance non admise", "cgi-2025-is-insurance-self-insurance", CGI, "fr"),
    ("dons et libéralités plafond 0,5 pour cent du chiffre d'affaires clubs sportifs", "cgi-2025-is-donations-cap", CGI, "fr"),
    ("intérêts servis aux associés sous-capitalisation taux BEAC deux points", "cgi-2025-is-financial-charges-thin-cap", CGI, "fr"),
    ("créances irrécouvrables déductibles épuisement des voies de recouvrement", "cgi-2025-is-bad-debt-and-losses", CGI, "fr"),
    ("amortissements différés en période déficitaire seuil petit matériel", "cgi-2025-is-depreciation-basis", CGI, "fr"),
    ("taux d'amortissement du matériel informatique et du mobilier de bureau", "cgi-2025-is-depreciation-rates", CGI, "fr"),
    ("provisions pour créances douteuses étalées sur deux ou trois ans", "cgi-2025-is-provisions", CGI, "fr"),
    ("pertes de change ne donnent pas lieu à des provisions déductibles", "cgi-2025-is-fx-provisions-non-deductible", CGI, "fr"),
    ("charges payées en espèces au-delà de 100 000 non déductibles", "cgi-2025-is-cash-payment-and-invoice-limits", CGI, "fr"),
    ("charges versées vers un paradis fiscal non déductibles", "cgi-2025-is-tax-haven-payments", CGI, "fr"),
    ("plus-values de cession ou cessation imposées moitié ou tiers cinq ans", "cgi-2025-is-capital-gains-cessation", CGI, "fr"),
    ("report du déficit pendant combien d'années établissements de crédit", "cgi-2025-is-loss-carryforward", CGI, "fr"),
    ("régime des sociétés mères et filiales quote-part de 10 pour cent", "cgi-2025-is-participation-exemption", CGI, "fr"),
    ("période d'imposition exercice de douze mois", "cgi-2025-is-period", CGI, "fr"),
    ("taux de l'impôt sur les sociétés 30 ou 25 chiffre d'affaires 3 milliards", "cgi-2025-is-rate", CGI, "fr"),
    ("prix de transfert entreprises liées seuil de contrôle 25 pour cent", "cgi-2025-is-transfer-pricing", CGI, "fr"),
    ("acompte mensuel d'impôt sur les sociétés régime réel et simplifié", "cgi-2025-is-installments-acompte", CGI, "fr"),
    ("minimum de perception base chiffre d'affaires de l'exercice précédent", "cgi-2025-is-minimum-tax", CGI, "fr"),
    ("déclaration annuelle des résultats DSF délais 15 mars avril mai", "cgi-2025-is-filing-obligations", CGI, "fr"),
    ("assiette de l'impôt sur les sociétés bénéfices des personnes morales", "cgi-2025-is-scope", CGI, "fr"),

    # ---- K12 — CGI withholding tax ----
    ("taux de l'impôt sur les revenus de capitaux mobiliers dividendes", "cgi-2025-wht-ircm-rate", CGI, "fr"),
    ("revenus de capitaux mobiliers imposables produits d'actions obligations", "cgi-2025-wht-ircm-base", CGI, "fr"),
    ("retenue à la source sur dividendes par le payeur réputés distribués neuf mois", "cgi-2025-wht-ircm-withholding", CGI, "fr"),
    ("revenus de capitaux mobiliers de source étrangère perçus au Cameroun", "cgi-2025-wht-ircm-foreign-source", CGI, "fr"),
    ("taux réduit pour les valeurs mobilières cotées à la BVMAC", "cgi-2025-wht-bvmac-securities-reduced", CGI, "fr"),
    ("retenue à la source sur les salaires par l'employeur dispense 62000", "cgi-2025-wht-salary-withholding", CGI, "fr"),
    ("barème progressif de l'IRPP sur les traitements et salaires", "cgi-2025-wht-irpp-salary-scale", CGI, "fr"),
    ("retenue à la source de 15 pour cent sur les revenus fonciers loyers", "cgi-2025-wht-property-income", CGI, "fr"),
    ("impôt foncier libératoire de 10 pour cent loyers hors retenue", "cgi-2025-wht-property-income-liberatoire", CGI, "fr"),
    ("prélèvement libératoire sur les plus-values immobilières notaire", "cgi-2025-wht-property-capital-gains", CGI, "fr"),
    ("retenue d'acompte par le comptable public sur la commande publique", "cgi-2025-wht-public-procurement-accountant", CGI, "fr"),
    ("acompte de 5 pour cent retenu à la source par l'État", "cgi-2025-wht-acompte-5pct", CGI, "fr"),
    ("retenue à la source par l'opérateur de plateforme numérique", "cgi-2025-wht-digital-platform", CGI, "fr"),
    ("retenue libératoire de 10 pour cent sur les agents commerciaux non salariés", "cgi-2025-wht-non-salaried-agents", CGI, "fr"),
    ("taux libératoires sur les revenus non commerciaux artistes plateformes", "cgi-2025-wht-bnc-liberatoire-rates", CGI, "fr"),
    ("collecte par retenue à la source sur la dépense publique avances", "cgi-2025-wht-public-expenditure-collection", CGI, "fr"),
    ("attestation de retenue à la source obligatoire", "cgi-2025-wht-attestation", CGI, "fr"),

    # ---- Second queries on high-traffic rules (robustness) ----
    ("comment amortir un ordinateur quel taux fiscal", "cgi-2025-is-depreciation-rates", CGI, "fr"),
    ("société déficitaire combien d'impôt minimum payer", "cgi-2025-is-minimum-tax", CGI, "fr"),
    ("quel est le taux normal de l'IS au Cameroun", "cgi-2025-is-rate", CGI, "fr"),
    ("combien d'années pour reporter une perte fiscale", "cgi-2025-is-loss-carryforward", CGI, "fr"),
    ("limite de déduction des frais de siège société étrangère", "cgi-2025-is-headquarters-and-technical-fees-cap", CGI, "fr"),
    ("paiement fournisseur en espèces est-il déductible plafond", "cgi-2025-is-cash-payment-and-invoice-limits", CGI, "fr"),
    ("dividendes reçus d'une filiale sont-ils imposables mère", "cgi-2025-is-participation-exemption", CGI, "fr"),
    ("retenue sur loyer payé par une société quel taux", "cgi-2025-wht-property-income", CGI, "fr"),
    ("taux de retenue sur dividendes distribués", "cgi-2025-wht-ircm-rate", CGI, "fr"),
    ("acompte mensuel à verser au régime réel pourcentage chiffre d'affaires", "cgi-2025-is-installments-acompte", CGI, "fr"),
    ("achat d'un terrain dans quelle classe de comptes", "syscohada-class-2", SYS, "fr"),
    ("provision pour un litige avec un client comment comptabiliser", "syscohada-eval-provisions", SYS, "fr"),
    ("évaluer un stock à la sortie quelle méthode", "syscohada-eval-inventory-fifo-or-wac", SYS, "fr"),
    ("remplacer l'ascenseur d'un immeuble traitement comptable", "syscohada-component-replacement-derecognition", SYS, "fr"),
    ("baisse de valeur d'une machine à la clôture", "syscohada-impairment-test-principle", SYS, "fr"),
    ("amortir un véhicule de tourisme sur quelle durée fiscale", "cgi-2025-is-depreciation-rates", CGI, "fr"),

    # ---- English block (measures the cross-lingual gap; corpus is French) ----
    ("corporate income tax rate Cameroon", "cgi-2025-is-rate", CGI, "en"),
    ("minimum tax for a loss-making company", "cgi-2025-is-minimum-tax", CGI, "en"),
    ("withholding tax on dividends rate", "cgi-2025-wht-ircm-rate", CGI, "en"),
    ("loss carry forward how many years", "cgi-2025-is-loss-carryforward", CGI, "en"),
    ("depreciation rate for computers", "cgi-2025-is-depreciation-rates", CGI, "en"),
    ("thin capitalisation interest to shareholders", "cgi-2025-is-financial-charges-thin-cap", CGI, "en"),
    ("parent subsidiary dividend exemption", "cgi-2025-is-participation-exemption", CGI, "en"),
    ("withholding tax on rent property income", "cgi-2025-wht-property-income", CGI, "en"),
    ("component approach fixed asset decomposition", "syscohada-component-decomposition-principle", SYS, "en"),
    ("impairment reversal cap on fixed assets", "syscohada-impairment-reversal-cap", SYS, "en"),
    ("inventory valuation FIFO weighted average", "syscohada-eval-inventory-fifo-or-wac", SYS, "en"),
    ("chart of accounts class for fixed assets", "syscohada-class-2", SYS, "en"),
    ("provisions for risks and charges", "syscohada-eval-provisions", SYS, "en"),

    # ---- Transaction-style queries (how a user / agent really phrases it) ----
    ("compte de trésorerie pour un virement bancaire reçu", "syscohada-class-5", SYS, "fr"),
    ("dette envers un fournisseur compte de tiers", "syscohada-class-4", SYS, "fr"),
    ("produit de la vente de marchandises quelle classe", "syscohada-class-7", SYS, "fr"),
    ("charge d'achat de fournitures de bureau quelle classe", "syscohada-class-6", SYS, "fr"),
    ("augmentation du capital social quelle classe de comptes", "syscohada-class-1", SYS, "fr"),
    ("engagements hors bilan donnés caution quelle classe", "syscohada-class-9", SYS, "fr"),
    ("comment évaluer une immobilisation produite par l'entreprise", "syscohada-eval-production-cost", SYS, "fr"),
    ("frais accessoires inclus dans le coût d'une machine", "syscohada-eval-acquisition-cost-immobilisation", SYS, "fr"),
    ("la réévaluation libre des immobilisations est-elle permise", "syscohada-eval-base-conventions", SYS, "fr"),
    ("intérêts d'emprunt pendant la construction d'un immeuble", "syscohada-eval-borrowing-costs-qualified-asset", SYS, "fr"),
    ("frais d'établissement à la première application du référentiel", "syscohada-fta-charges-immobilisees", SYS, "fr"),
    ("impact de la première application sur les capitaux propres", "syscohada-fta-change-of-method-retrospective", SYS, "fr"),
    ("indemnités de départ à la retraite non provisionnées à la transition", "syscohada-fta-retirement-commitments", SYS, "fr"),
    ("acquisition d'un immeuble avec un ascenseur à renouveler", "syscohada-component-decomposition-principle", SYS, "fr"),
    ("obligation de remise en état du site en fin d'exploitation", "syscohada-component-dismantling-asset", SYS, "fr"),
    ("un indice montre qu'une machine a perdu de la valeur", "syscohada-impairment-test-principle", SYS, "fr"),
    ("jusqu'à quel montant reprendre une dépréciation antérieure", "syscohada-impairment-reversal-cap", SYS, "fr"),
    ("déprécier un ensemble d'actifs comprenant un fonds commercial", "syscohada-impairment-group-allocation", SYS, "fr"),
    ("ma société a versé des honoraires d'assistance technique à l'étranger", "cgi-2025-is-headquarters-and-technical-fees-cap", CGI, "fr"),
    ("paiement de redevances de marque à la maison mère étrangère", "cgi-2025-is-royalties-cap", CGI, "fr"),
    ("un don à une association caritative est-il déductible", "cgi-2025-is-donations-cap", CGI, "fr"),
    ("une amende fiscale est-elle déductible du résultat", "cgi-2025-is-taxes-fines-deductibility", CGI, "fr"),
    ("provision pour une créance client douteuse déductibilité", "cgi-2025-is-provisions", CGI, "fr"),
    ("un associé prête de l'argent à la société intérêts déductibles", "cgi-2025-is-financial-charges-thin-cap", CGI, "fr"),
    ("cession du fonds de commerce imposition de la plus-value", "cgi-2025-is-capital-gains-cessation", CGI, "fr"),
    ("vente à une société liée à l'étranger ajustement du prix", "cgi-2025-is-transfer-pricing", CGI, "fr"),
    ("les frais de personnel et salaires sont-ils déductibles", "cgi-2025-is-remuneration-deductibility", CGI, "fr"),
    ("une société nouvelle peut-elle prolonger son premier exercice", "cgi-2025-is-period", CGI, "fr"),
    ("revenus exonérés des coopératives et établissements d'enseignement", "cgi-2025-is-exemptions", CGI, "fr"),
    ("versement de dividendes à un actionnaire quelle retenue", "cgi-2025-wht-ircm-withholding", CGI, "fr"),
    ("paiement d'un loyer commercial par une SARL au régime réel", "cgi-2025-wht-property-income", CGI, "fr"),
    ("rémunération d'un consultant non salarié retenue libératoire", "cgi-2025-wht-non-salaried-agents", CGI, "fr"),
    ("paiement à un prestataire sur un marché public retenue", "cgi-2025-wht-public-procurement-accountant", CGI, "fr"),
    ("intérêts d'obligations cotées en bourse régionale imposition", "cgi-2025-wht-bvmac-securities-reduced", CGI, "fr"),
    ("retenue sur le salaire mensuel d'un employé", "cgi-2025-wht-salary-withholding", CGI, "fr"),
    ("vente d'un immeuble par un particulier prélèvement du notaire", "cgi-2025-wht-property-capital-gains", CGI, "fr"),
    ("revenus tirés d'une plateforme numérique retenue par l'opérateur", "cgi-2025-wht-digital-platform", CGI, "fr"),
    ("jetons de présence des administrateurs déductibilité", "cgi-2025-is-remuneration-deductibility", CGI, "fr"),
    ("base de calcul du minimum d'impôt chiffre d'affaires N-1", "cgi-2025-is-minimum-tax", CGI, "fr"),
    ("amortissement non pratiqué en période de déficit report", "cgi-2025-is-depreciation-basis", CGI, "fr"),

    # ---- More English (cross-lingual gap) ----
    ("who pays corporate income tax companies", "cgi-2025-is-taxable-persons", CGI, "en"),
    ("tax exemptions for agricultural cooperatives", "cgi-2025-is-exemptions", CGI, "en"),
    ("deductible donations charitable limit", "cgi-2025-is-donations-cap", CGI, "en"),
    ("head office management fee deduction cap", "cgi-2025-is-headquarters-and-technical-fees-cap", CGI, "en"),
    ("salary income tax brackets scale", "cgi-2025-wht-irpp-salary-scale", CGI, "en"),
    ("transfer pricing related party transactions", "cgi-2025-is-transfer-pricing", CGI, "en"),
    ("provision for doubtful debts deduction", "cgi-2025-is-provisions", CGI, "en"),
    ("dismantling cost provision component asset", "syscohada-component-dismantling-asset", SYS, "en"),
    ("deemed distribution of dividends nine months", "cgi-2025-wht-ircm-withholding", CGI, "en"),
    ("major inspection overhaul component", "syscohada-component-major-revision", SYS, "en"),
    ("group of assets impairment allocation", "syscohada-impairment-group-allocation", SYS, "en"),
    ("monthly installment advance payment turnover", "cgi-2025-is-installments-acompte", CGI, "en"),

    # ---- Final coverage top-up (to 200) ----
    ("comptabiliser l'acquisition d'un logiciel comme immobilisation", "syscohada-class-2", SYS, "fr"),
    ("calcul de l'impôt sur les sociétés sur le bénéfice fiscal", "cgi-2025-is-rate", CGI, "fr"),
    ("retenue à la source sur prestations payées par l'État", "cgi-2025-wht-public-procurement-accountant", CGI, "fr"),
    ("l'amortissement dégressif fait partie des modes admis", "syscohada-eval-depreciation", SYS, "fr"),
    ("intérêts sur compte courant d'associé plafond déductible", "cgi-2025-is-financial-charges-thin-cap", CGI, "fr"),
    ("conversion d'une créance libellée en devises à l'entrée", "syscohada-eval-fx-on-entry", SYS, "fr"),
    ("date limite de déclaration des résultats grandes entreprises", "cgi-2025-is-filing-obligations", CGI, "fr"),
    ("territorialité société étrangère avec établissement stable", "cgi-2025-is-territoriality", CGI, "fr"),
    ("fixed asset component separate depreciation", "syscohada-component-separate-depreciation", SYS, "en"),
    ("irrecoverable bad debt write off deduction", "cgi-2025-is-bad-debt-and-losses", CGI, "en"),
    ("opening balance sheet on first adoption", "syscohada-fta-opening-balance-sheet", SYS, "en"),
    ("foreign source investment income withholding", "cgi-2025-wht-ircm-foreign-source", CGI, "en"),
]


def _rank_of(expected, results):
    for i, r in enumerate(results, start=1):
        if r["slug"] == expected:
            return i
    return None


def evaluate(k=5, subset=None):
    """Run the test set through retrieve() and return a metrics dict.

    ``subset`` optionally filters entries by a predicate (entry tuple) → bool.
    Imported lazily so importing this module never pulls Django at import time.
    """
    from knowledge.retrieval import retrieve

    entries = [e for e in TEST_SET if (subset is None or subset(e))]
    buckets = {}  # name -> list of ranks (None if missed)

    def record(name, rank):
        buckets.setdefault(name, []).append(rank)

    for query, expected, framework, lang in entries:
        results = retrieve(query, framework=framework, k=k,
                           only_effective=False)
        rank = _rank_of(expected, results)
        record("overall", rank)
        record(f"fw:{framework}", rank)
        record(f"lang:{lang}", rank)

    def summarise(ranks):
        n = len(ranks)
        if not n:
            return None
        hit1 = sum(1 for r in ranks if r == 1)
        hit3 = sum(1 for r in ranks if r and r <= 3)
        hit5 = sum(1 for r in ranks if r and r <= 5)
        mrr = sum((1.0 / r) for r in ranks if r) / n
        return {
            "n": n,
            "p_at_1": hit1 / n,
            "r_at_3": hit3 / n,
            "r_at_5": hit5 / n,
            "mrr": mrr,
            "misses": [i for i, r in enumerate(ranks) if r is None],
        }

    return {name: summarise(ranks) for name, ranks in buckets.items()}
