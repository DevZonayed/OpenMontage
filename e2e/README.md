# Production command-center browser acceptance (E2E)

A committed, repeatable real-browser gate for the board + Studio production
command center. It drives the **actual served pages** (including
`/ui/studio.bundle.js`) in headless Chromium at **1440×900 and 1920×1080** against
the exact fixture that was previously rejected, and asserts the visible
full-document text and behavior — the "tests passed / real browser failed" path.

## What it asserts (both viewports, board + Studio)

- No legacy leakage anywhere in the document: `fake_driver`, `NO LIVE RUN`,
  `NOT STARTED`, `brain: —`, `DETERMINISTIC FIXTURE`, `OFFLINE DRIVER`.
- Exactly **one** enabled prominent primary action (one Connect on the board; one
  `cc-primary` in the Studio); **no** enabled `Start production`.
- `Render final film` is **disabled** (no renderable layers).
- Truthful target duration `2:30 · 4500 target frames` — never the `1:00 / 1800`
  composer default.
- Board headline == Studio command-center headline == Studio inspector headline.
- Zero console/page errors.

## Run it

```sh
# 1. deps (once): a cached Chromium + the driver
npm --prefix remotion-composer i -D playwright-core   # or: npm i -D playwright-core

# 2. build the shipped bundle the Studio serves
npm --prefix remotion-composer run build:studio

# 3. create the exact fixture
OPENMONTAGE_PROJECTS_DIR=/tmp/openmontage-acceptance-projects \
  .venv/bin/python e2e/setup_fixture.py

# 4. serve it (background), Hermes intentionally disconnected
OPENMONTAGE_PROJECTS_DIR=/tmp/openmontage-acceptance-projects \
  .venv/bin/python -m backlot serve --port 8894 &

# 5. run the acceptance
BASE=http://127.0.0.1:8894 node e2e/studio-acceptance.mjs
```

The script exits non-zero on any assertion failure and prints the failures. If
`playwright-core` is not installed it SKIPS with exit 0 (so a browserless CI does
not hard-fail); install it to actually run the gate.

A CI-safe companion runs in the normal Vitest suite:
`remotion-composer/src/studio/studio-bundle.test.tsx` loads the SAME shipped
`studio.bundle.js` into jsdom and asserts the same content invariants without a
browser download.
