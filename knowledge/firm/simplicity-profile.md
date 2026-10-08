# Firm Profile: Simplicity Wealth (Acquiring Firm)

## Overview
- **Firm Name:** Simplicity Wealth
- **Role:** Acquiring wealth-management platform
- **Primary Custodian:** Charles Schwab

## Operational Architecture
- **CRM & Client Administration:** Salesforce SPARC (manages households, clients, and custodial account metadata)
- **Trading & Portfolio Accounting:** Orion Eclipse (TAMP platform handling model aggregates, sleeve management, rebalancing, and trade blotters)

## Target System Boundaries
- **SPARC:** Does not handle live trading or portfolio accounting.
- **Orion Eclipse:** Receives sleeve compositions, core model references, position snapshots, and trade blotters.