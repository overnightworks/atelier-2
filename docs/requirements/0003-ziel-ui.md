# Requirement 0003: Ziel-UI — eine Werkstatt, vier Räume, ein Graph

## Intent

Der Operator sieht jeden Lauf als lebenden Graphen — event-sourced, gestreamt —
mit Attempts optional als ephemeren Kacheln, die mit dem Attempt entstehen und
enden, nie als Dauersitze; Eingreifen nutzt die native Interaktivität der
Provider-Konsole, nichts davon wird nachgebaut (Issue #9 body). Um diesen Kern
sitzt die Ziel-UI nach Mockup v9: eine Werkstatt, geordnet durch eine Rail mit
vier Räumen — Home, Runs, Catalog, Settings — und dem Sitz als Mitte auf Home
und Catalog (ADR 0019 samt seiner Ergänzung vom 11.09.2026). Mockup v9 ist der
Gestalt-Owner.

Der Maßstab des Operators, wörtlich (5301898411): „simple — ich kann alles
machen was ich will — alles einstellen wenn ich will. Intuitiv und es muss
Spaß machen und cool aussehen. Und natürlich alles funktionieren." Sein
Qualitätsvertrag, wörtlich (Issue #336 body): „klar strukturiert, intuitiv,
einfach zu bedienen, kein unnützer Schnickschnack, ich muss schnell finden was
ich brauche — es darf trotzdem geil aussehen und Spaß machen." Das
Zulassungskriterium für alles Weitere: es muss Arbeit wegnehmen, nicht
Features hinzufügen (5302788411).

## Rules

### REQ-UI-20: Die Werkstatt ordnet sich in eine Rail mit drei Räumen — Workbench, Catalog, History — und am Fuß dem Raum Settings, dem Kontext über den dreien, mit dem Projektnamen klein darunter: Projekt-Umschalter im Kopf, verbundene Quellen, Modell-Registry je Provider, Model defaults je Stufe. Ein Profile-Platz kommt mit der Entscheidung von #82 und #106 (REQ-UI-15), nie vorher.
Quelle: OPERATOR — ADR 0019 (Operator-Segnung 25.08.2026, #711), löst REQ-UI-01 ab; superseded-by REQ-UI-26 (Operator-Segnung 11.09.2026, #1543)

### REQ-UI-26: Die Werkstatt ordnet sich in eine Rail mit vier Räumen — Home · Runs · Catalog · Settings; der Sitz ist die Mitte auf Home und Catalog und sonst nicht da.
Quelle: OPERATOR — Mockup v9, Ruling 3 und die Rail der Runde 4.2 (Operator-Segnung 11.09.2026, #1543)

### REQ-UI-21: Jeder Bildschirm beantwortet genau eine Frage; warten mehrere Fragen, ist eine als Bühne OFFEN und die anderen sind darunter in einem kompakten Stapel erreichbar — nie hinter einer Zahl verborgen.
Quelle: DESK — 5302788411 (Regel 2); Bühne und Stapel per ADR 0019 (Operator-Segnung 25.08.2026, #711), löst REQ-UI-02 ab; superseded-by REQ-UI-29 (Operator-Segnung 11.09.2026, #1543)

### REQ-UI-29: Warten ist als Zeile gestaltet, nicht als Bühne: der Kopf `workflow@rev · wait <node> ›` führt in den Lauf an genau diesen Knoten, darunter stehen die Frage und ihre Antworten; warten mehrere, stehen sie als gleichrangige Zeilen untereinander, und mobil öffnet die eine Zeile `N asks ›` dieselben Zeilen als Blatt.
Quelle: OPERATOR — Mockup v9, Ruling 2 und der Runde-4.2-Satz zu Asks (Operator-Segnung 11.09.2026, #1543)

### REQ-UI-22: Workflows entstehen agentisch, nie in einem Baukasten oder Editor; ein neuer Entwurf erscheint als Karte im Catalog, und der Operator segnet ihn dort ab.
Quelle: OPERATOR — Operator-Ruling 22.08.2026 (Epic #516); Catalog statt Board per ADR 0019 (Operator-Segnung 25.08.2026, #711), löst REQ-UI-03 ab; superseded-by REQ-UI-28 (Operator-Segnung 11.09.2026, #1543)

### REQ-UI-28: Der Catalog ist Sicht und Bauplatz: drei Regale mit der Herkunft `repo@commit` aus verbundenen Catalog sources, ein Entwurf wächst am Sitz und wird durch die Push-Tür ins Catalog-Repository gespeichert; es gibt keinen Import.
Quelle: OPERATOR — Mockup v9, Ruling 4 und die Löschung des Import-Wegs („ja löschen") (Operator-Segnung 11.09.2026, #1543)

### REQ-UI-23: Woran die Flotte arbeitet, ist erstklassig: jeder Raum ist projekt-gescoped, und der Projekt-Umschalter im Kopf von Settings ist die eine Naht: derselbe Klick wechselt das Projekt und landet in dessen Settings.
Quelle: DESK — 5301898411 §1; Naht in Settings per ADR 0019 (Operator-Segnung 25.08.2026, #711), löst REQ-UI-04 ab

### REQ-UI-05: Die Bibliothek zeigt Namen, nie Hashes.
Quelle: DESK — 5301898411 §3

### REQ-UI-06: Derselbe Graph ist die Live-Sicht: ein Renderer in drei Zeitformen — still, live, eingefroren.
Quelle: DESK — 5301898411 §4, 5302066517 §2; drei Zeitformen per Mockup v5 (Operator-Ruling 22.08.2026)

### REQ-UI-07: Der Modus ist eine Fähigkeits-Erklärung.
Quelle: DESK — #9 body @ 36800d6ecd5d3e8922028425835b368b42d163098e5d32da930e40d25f49ce99, Kommentar 5294316639

### REQ-UI-08: Interaktives Attach ist in V1 nur lokal, und es markiert den Lauf.
Quelle: DESK — #9 body @ 36800d6ecd5d3e8922028425835b368b42d163098e5d32da930e40d25f49ce99 Teil 2, 5302132001 §2 Schluss

### REQ-UI-09: Die Live-Kachel liest den ephemeren Runner-Kanal; rohe Provider-Frames erreichen nie ein Ereignis oder ein Receipt.
Quelle: DESK — 5302132001 §2

### REQ-UI-10: Abgeschlossene Läufe bekommen Historien-Wiedergabe, keine byte-genaue.
Quelle: DESK — 5302132001 §1

### REQ-UI-11: V1 baut Projekt-Gedächtnis; Gedächtnis je Benutzer hängt an #82.
Quelle: DESK — 5302132001 §3

### REQ-UI-12: Die UI-Sprache ist komplett Englisch, mit kurzen Namen.
Quelle: OPERATOR — Operator-Ruling 22.08.2026 (Epic #516), löst „Englisch als Vorgabe, Deutsch optional" aus 5302066517 §1 ab

### REQ-UI-13: Die Run-Liste zeigt Zweck und Ergebnis in Klartext, nie nur Status.
Quelle: DESK — 5302066517 §4; Klartext-Ergebnis per Mockup v5 (Operator-Ruling 22.08.2026)

### REQ-UI-14: Ein austauschbares Design-Token-System: Struktur ist strikt von Skin getrennt, und Komponenten konsumieren ausschließlich Tokens.
Quelle: DESK — 5302066517 §5, geschärft durch Operator-Ruling 22.08.2026 (Epic #516, Token-Schicht)

### REQ-UI-15: Einstellungen sind eine professionelle Fläche ohne hartkodierte Provider-Zeilen.
Quelle: DESK — 5302066517 §6, auf den Operator-Satz „wie sie aufgebaut ist mag ich noch nicht"; die Fläche per Mockup v9: Settings › Sources in zwei Gruppen (Project sources, Catalog sources), Automation als Regel-Liste, Models nach Schwierigkeit (Operator-Segnung 11.09.2026, #1543)

### REQ-UI-24: Die Werkstatt nimmt Arbeit weg: lehrende Leerzustände, das Receipt als Schmuckstück, Rückgängig statt Nachfragen; Puls-Kopfzeile und Posteingang gehen in der Workbench auf — die Bühne ist der Posteingang, die Ocker-Zahl in der Rail der Puls, die Queue eine aufklappbare Zeile.
Quelle: DESK — 5302788411; Workbench statt Board per ADR 0019 (Operator-Segnung 25.08.2026, #711), löst REQ-UI-16 ab; superseded-by REQ-UI-27 (Operator-Segnung 11.09.2026, #1543)

### REQ-UI-27: Ein menschliches Tor ist immer ein deklarierter `wait`-Knoten — dieselbe Zeile mit denselben Antworten auf Home und im Lauf; Home rechts trägt Asks und einen Block Runs mit Status-Chips, keine Queue-Knöpfe.
Quelle: OPERATOR — Mockup v9, Ruling 1, Ruling 2 und die Runde-4.2-Sätze zu Asks und Runs (Operator-Segnung 11.09.2026, #1543)

### REQ-UI-17: Authentifizierung erhöht die Komplexität keines Bildschirms.
Quelle: DESK — 5302788411 (Regel 17)

### REQ-UI-25: Gegen die gesegnete Vorlage wird gebaut, und ihre Tore werden gemessen statt behauptet; der aktuelle Stand ist [Mockup v9](0003-ziel-ui-mockup-v9.html), Owner-Record ADR 0019, wie Code per PR geändert; jede gesegnete Fassung wird eingefroren, die neueste gesegnete ist der Owner.
Quelle: DESK — 5302769095, 5302066517 Schluss; Vorlage v9 gesegnet 11.09.2026 (ADR 0019, Ergänzung vom 11.09.2026, #1543), löst REQ-UI-18 ab

### REQ-UI-19: Atelier 1 wird als Konzepte und Lehren wiederverwendet, nie als portierter Code.
Quelle: DESK — #9 body @ 36800d6ecd5d3e8922028425835b368b42d163098e5d32da930e40d25f49ce99 (Regel 19)

### REQ-UIQ-02: Kernaufgaben erreichen ihr Ziel in einem benannten Klick- und Blick-Budget.
Quelle: DESK — #336 body @ 92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8, Kriterium 2

### REQ-UIQ-03: Begriffe einer Fläche kommen aus einer Quelle.
Quelle: DESK — #336 body @ 92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8, Kriterium 3

### REQ-UIQ-12: Anzeige-Strings eines Raums kommen aus ihrem Owner, und das Layout verträgt die längere Form; Kernflächen sind die drei Räume, Settings und die Run-Sicht.
Quelle: DESK — #336 body @ 92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8, Kriterium 4; Kernflächen-Liste per ADR 0019 (Operator-Segnung 25.08.2026, #711), löst REQ-UIQ-04 ab

### REQ-UIQ-05: Die Kernflächen erfüllen WCAG 2.2 AA, oder der Verstoß trägt ein Item.
Quelle: DESK — #336 body @ 92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8, Kriterium 5

### REQ-UIQ-10: Leer, lädt, Fehler und wartet sind vier gestaltete Zustände — benannt heißt gestaltet, nicht beschriftet: Leer ist die Form ohne Inhalt, Laden ein stilles Skelett, Fehler Brick mit einem Satz und einem Zug, Warten die Bühne.
Quelle: DESK — #336 body @ 92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8, Kriterium 6; gestaltet statt benannt per ADR 0019 (Operator-Segnung 25.08.2026, #711), löst REQ-UIQ-06 ab; das vierte Wort (Warten) superseded-by REQ-UI-29 (Operator-Segnung 11.09.2026, #1543)

### REQ-UIQ-07: Eine Frage hat ein Muster, und das Muster ist eine wiederverwendete Komponente.
Quelle: DESK — #336 body @ 92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8, Kriterium 7

### REQ-UIQ-13: Ein Verhalten hat flächenübergreifend genau eine Komponente; eine zweite Implementierung desselben Verhaltens ist ein Defekt.
Quelle: OPERATOR — 31.08.2026, wörtlich „neue Sachen sollen sich immer gleich verhalten — wenn ich eine Suche mit Highlight habe, will ich an anderer Stelle das gleiche Verhalten, und ändere ich es an einer Stelle, passt es sich automatisch an den anderen an", Segnung „ja wenn es geht auf jeden Fall"; vgl. REQ-UIQ-07 (Muster pro Frage, hier flächenübergreifend)

### REQ-UIQ-08: Eine Fläche, die ihr Interaktions-Budget überschreitet, ist ein Defekt.
Quelle: DESK — #336 body @ 92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8, Kriterium 8

### REQ-UIQ-11: Die Fläche darf geil aussehen und Spaß machen; der Screenshot-Maßstab ist Mockup v8, und das letzte Wort hat der Operator.
Quelle: OPERATOR — #336 body @ 92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8, wörtlich „es darf trotzdem geil aussehen und Spaß machen"; Maßstab v8 per ADR 0019 (Operator-Segnung 25.08.2026, #711), löst REQ-UIQ-09 ab; superseded-by REQ-UIQ-14 (Operator-Segnung 11.09.2026, #1543)

### REQ-UIQ-14: Die Fläche darf geil aussehen und Spaß machen; der Screenshot-Maßstab ist Mockup v9, und das letzte Wort hat der Operator.
Quelle: OPERATOR — #336 body @ 92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8, wörtlich „es darf trotzdem geil aussehen und Spaß machen"; Maßstab v9 per Mockup v9 (Operator-Segnung 11.09.2026, #1543)

## Non-goals

Kein Workflow-Editor und keine Baukasten-Tür — Workflows entstehen agentisch
(Operator-Ruling 22.08.2026). Keine Dashboards, kein Board und kein
Benachrichtigungszentrum: die Asks auf Home sind die Benachrichtigung (ADR
0019 samt seiner Ergänzung vom 11.09.2026). Die ⌘K-Befehlspalette und die
Multi-Run-Kachelwand sind benannte Nachfolger, kein V1-Bau (5302132001).
Remote-Attach ist ein eigenes Epic hinter einer Runner-Trust-Entscheidung;
nichts backt localhost ein, aber V1 baut es nicht (Issue #9 body, Teil 3).
Technische Heimaten — Katalog-Identität, Projekt-Isolation, Attach-Weg —
besitzen die ADRs, nicht dieses Dokument.
