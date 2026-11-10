"""Bounded saved-plan context, never an authorization or execution instruction."""
from transaction_store import Store

def snapshot(root):
    inventory=Store(root).inventory()
    # Uncertain writes take precedence over ordinary previews.
    rows=sorted(inventory['transactions'],key=lambda r:not r['requiresRecovery'])[:10]
    return {'scope':'Saved mixer transaction records, not a fresh Live observation',
            'transactions':rows,'moreRecords':inventory['truncated'] or len(inventory['transactions'])>10,
            'policy':'Saved plans and matching values do not authorize execution, retries, recovery resolution or musical changes. Unverified execution remains disabled.'}

def english(context):
    rows=context['transactions']
    if not rows:return 'No saved mixer plans. Use Session plans & recovery to review a proposed change.'
    lines=['Saved mixer plans — these are records, not proof of the current Live state.']
    for row in rows:
        lines.append(row['status'].replace('-',' ').capitalize()+': '+'; '.join(row['summary']))
        if row['requiresRecovery']:lines.append('Recovery inspection is required. No write has been retried or journal resolved.')
    if context['moreRecords']:lines.append('More records are available in Session plans & recovery.')
    lines.append('Preview means proposed only. New transaction execution remains gated.')
    return '\n'.join(lines)
