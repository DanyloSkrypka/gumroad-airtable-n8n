// Builds a readable alert from the Error Trigger output.
// Runs for: Airtable write failed (after 3 retries), invalid Gumroad payload, any other crash.
const e = $input.first().json;
const exec = e.execution || {};
const trig = e.trigger || {};
const err = exec.error || trig.error || {};
const wf = (e.workflow && e.workflow.name) || 'Gumroad -> Airtable';

const subject = `[n8n] ${wf} failed: ${(err.message || 'unknown error').slice(0, 120)}`;
const text = [
  `Workflow: ${wf}`,
  `Failed node: ${exec.lastNodeExecuted || '(trigger)'}`,
  `Error: ${err.message || 'unknown'}`,
  exec.url ? `Open the failed run: ${exec.url}` : null,
  exec.id ? `Execution ID: ${exec.id}` : null,
  `Time (UTC): ${new Date().toISOString()}`,
  '',
  'This event may be missing in Airtable. Fix the cause, then either retry the execution',
  'from the link above or run the "Gumroad -> Airtable: backfill & reconcile" workflow,',
  'which re-syncs recent sales without creating duplicates.',
]
  .filter((l) => l !== null)
  .join('\n');

return [{ json: { subject, text } }];
