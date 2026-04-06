# Security Policy

## Reporting a Vulnerability

Do not report undisclosed vulnerabilities in public GitHub issues.

Use GitHub Security Advisories or the repository's private vulnerability
reporting flow for sensitive reports.

## What to Report

Please report vulnerabilities involving:

- unauthorized access to Orthanc or workflow endpoints
- credential exposure or unsafe defaults that are not clearly documented
- unsafe file handling, archive handling, or command execution paths
- vulnerabilities in CI, supply-chain, or packaging configuration in this repository

## Out of Scope

The following are generally out of scope unless they create a concrete exploit path:

- requests to support obsolete operating systems or unsupported Python versions
- expected behavior in clearly documented test-only configurations
- issues in third-party infrastructure that are not caused by this repository

## Supported Security Baseline

This repository documents and ships examples for local testing and operations.
Some examples are intentionally fast-to-start and not hardened for exposed,
shared, or long-term deployments. When such examples exist, they must be
explicitly documented as test-only.

## Response Expectations

Maintainers will triage security reports privately when possible, reproduce the
issue, determine impact, and coordinate a fix or mitigation before public
disclosure.
