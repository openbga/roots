# Security policy

## Reporting a vulnerability

Please report security problems privately. Do not open a public issue, pull request or
discussion for them.

Use GitHub's private vulnerability reporting: open the repository's
[Security tab](https://github.com/openbga/roots/security) and choose **Report a vulnerability**,
or go straight to <https://github.com/openbga/roots/security/advisories/new>.

Include what you can of the following:

- the affected page, endpoint, command or file, and the commit or version you tested;
- the role you were signed in as (unauthenticated, client portal user, or a staff role);
- steps to reproduce, and what you were able to read or change;
- any suggested fix.

Use synthetic data only. Never include real laboratory, client or personal data in a report,
and do not test against a deployment you do not operate.

Reports are handled on a best-effort basis by the maintainers. We will confirm receipt, keep
you informed while we investigate, and credit you in the advisory unless you prefer otherwise.
There is no bug bounty.

## Supported versions

The project has not yet made a stable release. Security fixes are published on the `main`
branch only. Deployments should track `main` and apply updates promptly.

## Scope

In scope: this repository's application code, configuration, container image definition and
deployment Blueprint.

Out of scope: vulnerabilities in a third-party dependency with no impact specific to this
project (report those upstream), findings that require an administrator or database owner to
attack their own deployment, and the configuration of deployments operated by others.

## Notes for operators

Each deployment is operated by the laboratory that runs it. The operator is responsible for
its secrets, HTTPS configuration, user accounts, multi-factor authentication policy, backups and
updates. Two documented design limits matter when assessing risk:

- Authenticator-app secrets and recovery codes are stored unencrypted in the database, so
  database dumps must be encrypted and access to them restricted.
- History tables reject updates and deletes through database triggers, but a database owner or
  superuser can disable them. This is not tamper evidence against database administrators.

See [Operations](docs/operations.md) for both.
