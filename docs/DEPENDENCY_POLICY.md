# Dependency Policy

MapGo intentionally has no production npm dependencies. Runtime code uses Node built-in modules, including `node:sqlite`, so production installs do not require `npm install`.

The only JavaScript package declared today is the Playwright dev dependency used by `npm run test:e2e`. CI installs it on demand before running E2E smoke tests.

For that reason the repository currently does not keep a `package-lock.json`. If production dependencies are added later, add and commit a lockfile in the same change.
