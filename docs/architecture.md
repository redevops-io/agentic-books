# Architecture

## OSS Core
- **ERPNext**: Provides ERP/accounting backend.
- **Beancount**: Plain-text Git-native double-entry ledger.
- **Frappe Books**: Desktop/mobile note.

## Agent Layer Pipeline
Receipt/Invoice Intake → OCR & Extraction → Categorization → Reconciliation → Journal Entry → Close Checklist → Variance Analysis → Financial Report → Owner Dashboard

With Human Review Queue at critical steps.

```mermaid
graph TD
    Intake[Receipt/Invoice Intake] --> OCR[OCR & Extraction]
    OCR --> Cat[Categorization]
    Cat --> Rec[Reconciliation]
    Rec --> JE[Journal Entry]
    JE --> CC[Close Checklist]
    CC --> VA[Variance Analysis]
    VA --> FR[Financial Report]
    FR --> Dash[Owner Dashboard]
    Cat --> HRQ[Human Review Queue]
    Rec --> HRQ
    JE --> HRQ
```

## Agent Service
The agent service communicates with ERPNext and Beancount via their respective APIs/clients, and sends LLM calls to the configured OpenAI-compatible endpoint.