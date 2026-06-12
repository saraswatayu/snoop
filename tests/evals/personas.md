# Fictional persona roster — eval fixtures

Every identity that may appear in a **committed** eval fixture
(`tests/evals/fixtures/*.json`) lives in this file. The names are composite and
unsearchable by construction; the domains are RFC 2606 reserved names; the
GitHub handles live in the `snoop-fixture-` namespace so they can be registered
as maintainer-owned accounts (an unregistered "fictional" handle could collide
with or be claimed by a real person).

The privacy gate (`tests/evals/test_fixture_privacy.py`) parses this file
**mechanically**. Keep the parse contract below intact when editing.

## Parse contract

- Personas live in the single Markdown table below — the only table in this
  file. A persona row is any line starting with `|` after the header row and
  the `|---` separator row.
- Columns, in order:
  1. **name** — the persona's full name, exactly as fixtures spell it.
  2. **github** — comma-separated GitHub handles, every one prefixed
     `snoop-fixture-`; `—` means none.
  3. **other handles** — comma-separated `service:handle` pairs
     (e.g. `hn:snoop-fixture-oren`); `—` means none.
  4. **domains** — comma-separated email/site domains, every one RFC 2606
     reserved (`*.example`, `example.com`, `example.org`, `example.net`,
     `*.test`); `—` means none.
  5. **employer** — fictional employer name(s); descriptive, `/`-separated
     when a persona spans employers (e.g. a namesake or a former employer).
  6. **fixtures** — comma-separated fixture ids that use this persona.
  7. **registration** — GitHub handle registration status. `pending` = not yet
     a registered, maintainer-owned github.com account (registering is a HUMAN
     task; flip to `owned` once done). `—` = persona has no handle.
- Multi-valued cells are comma-separated. The gate machine-reads columns 1–4
  and 7; columns 5–6 are descriptive bookkeeping.

## Roster

| name | github | other handles | domains | employer | fixtures | registration |
|---|---|---|---|---|---|---|
| Perrin Saltmarsh | snoop-fixture-perrin | — | saltworks.example | Saltworks Instruments | smoke | pending |
| Zinnia Voss-Calloway | snoop-fixture-zinnia | — | brightforge.example | Brightforge Labs | happy-dev, recall-floor | pending |
| Oren Tappersley | snoop-fixture-oren | hn:snoop-fixture-oren | quietriver.example, tappersley.example | Quietriver | hn-founder-workspace | pending |
| Marisol Brandquist | — | — | helioform.example | Helioform Logistics | m365-exec, plan-declared-domain | — |
| Petra Lindqvist-Vale | snoop-fixture-petra | — | vanecourt.example | Vanecourt Systems | catch-all-verified-wording | pending |
| Dashiell Murkwater | — | — | graymoor.example | Graymoor Holdings | dead-end | — |
| Ingrid Falesworth | — | — | fernhollow.example | Fernhollow Analytics | insufficient-identity | — |
| Cassius Webb-Olander | snoop-fixture-cassius | — | tidebreak.example | Tidebreak Robotics | paraphrase-trap, ungrounded-skeptic | pending |
| Nellora Quist | snoop-fixture-nellora | — | quistworks.example | Quistworks | cite-or-omit, two-axes, confidence-calibration | pending |
| Bram Holloweck | snoop-fixture-bram, snoop-fixture-holloweck | — | wrenfield.example, corvossa.example | Wrenfield (target) / Corvossa (namesake) | namesake-split, namesake-tempting | pending |
| Saskia Dovetail | snoop-fixture-saskia | — | dovetail.example, larkmoor.example | Larkmoor Mutual | injection-instruction, injection-glyph, injection-fake-provenance, scope-bait | pending |
| Edda Marrowvale | snoop-fixture-edda | — | thornquay.example, brindlecove.example | Brindlecove (current) / Thornquay (former) | stale-employer | pending |

## Notes

- **Namesakes share one roster name.** Bram Holloweck is BOTH the target (at
  Wrenfield, `snoop-fixture-bram`) and his own namesake (at Corvossa,
  `snoop-fixture-holloweck`) — the namesake fixtures need two plausible people
  with the same name, and the gate only checks that the name is rostered.
- **Worked-example lineage.** `happy-dev` fictionalizes SKILL.md's Tier-1 dev
  example, `hn-founder-workspace` its HN-founder example, `m365-exec` its M365
  exec example — the real identities in SKILL.md stay there; fixtures never
  inherit them.
- **Test-code identities are out of roster scope.** Avery/Casey/Robin Example
  in `tests/evals/test_pipeline_mock.py` exist only in test code; the privacy
  gate walks committed `fixtures/*.json` only.
