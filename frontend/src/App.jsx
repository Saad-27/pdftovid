import { useEffect, useState } from 'react';

const STAGE_LABELS = {
  A: 'Checking your file…',
  B: 'Reading your slides…',
  C: 'Understanding your lecture…',
  D: 'Writing the narration…',
  E: 'Recording the voiceover…',
  F: 'Planning the video…',
  G: 'Putting it all together…',
};

const FRIENDLY_ERRORS = {
  KEY_EXPIRED:
    'This job expired before it could be processed. Your API key was discarded ' +
    'for security and never stored. Please upload again to retry.',
};

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000';

export default function App() {
  const [apiKey, setApiKey] = useState('');
  const [voice, setVoice] = useState('');
  const [voices, setVoices] = useState([]);
  const [file, setFile] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [jobId, setJobId] = useState(null);
  const [job, setJob] = useState(null);

  // Load voices once on mount.
  useEffect(() => {
    fetch(`${API_BASE}/api/voices`)
      .then((r) => r.json())
      .then((vs) => {
        setVoices(vs);
        if (vs.length && !voice) setVoice(vs[0].id);
      })
      .catch(() => setError('Could not reach the backend. Is it running on :8000?'));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Poll the job status while we have an active job that isn't terminal.
  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;

    const tick = async () => {
      try {
        const r = await fetch(`${API_BASE}/api/jobs/${jobId}`);
        if (!r.ok) {
          setError(`Status request failed (${r.status}).`);
          return;
        }
        const data = await r.json();
        if (cancelled) return;
        setJob(data);
        // Stop polling on terminal states.
        if (data.state === 'done' || data.state === 'failed') return;
        setTimeout(tick, 2000);
      } catch (e) {
        if (!cancelled) setError(e.message);
      }
    };

    tick();
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    if (!file) {
      setError('Please choose a PDF.');
      return;
    }
    if (!apiKey.trim()) {
      setError('Please paste your Anthropic API key.');
      return;
    }

    setSubmitting(true);
    try {
      const form = new FormData();
      form.append('pdf_file', file);
      form.append('api_key', apiKey);
      form.append('voice', voice);

      const r = await fetch(`${API_BASE}/api/jobs`, { method: 'POST', body: form });
      const data = await r.json();
      if (!r.ok) {
        // FastAPI puts our structured error in detail.
        const msg = data?.detail?.message || data?.detail || `Request failed (${r.status})`;
        setError(msg);
        return;
      }
      setJobId(data.job_id);
      setJob(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setSubmitting(false);
    }
  };

  const reset = () => {
    setJobId(null);
    setJob(null);
    setError(null);
  };

  const terminal = job && (job.state === 'done' || job.state === 'failed');

  const videoSrc =
    job && job.state === 'done' && job.video_url
      ? job.video_url.startsWith('http')
        ? job.video_url
        : `${API_BASE}${job.video_url}`
      : null;

  return (
    <div className="app">
      <h1>Lecture Video</h1>
      <p className="subtitle">Turn a PDF of slides into a narrated MP4.</p>

      {!jobId && (
        <form onSubmit={submit}>
          <label htmlFor="api-key">
            Anthropic API key
            <div className="help">
              Used only to call Anthropic. Never stored or logged.
            </div>
          </label>
          <input
            id="api-key"
            type="password"
            placeholder="sk-ant-..."
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            autoComplete="off"
          />

          <label htmlFor="pdf">PDF of slides</label>
          <input
            id="pdf"
            type="file"
            accept="application/pdf"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />

          <label htmlFor="voice">Voice</label>
          <select id="voice" value={voice} onChange={(e) => setVoice(e.target.value)}>
            {voices.map((v) => (
              <option key={v.id} value={v.id}>
                {v.name}
              </option>
            ))}
          </select>

          <button type="submit" disabled={submitting}>
            {submitting ? 'Submitting…' : 'Generate Video'}
          </button>

          {error && <div className="error">{error}</div>}
        </form>
      )}

      {jobId && (
        <div className="status">
          <strong>Job {jobId.slice(0, 8)}…</strong>
          <div className="stage">
            {job
              ? job.state === 'failed'
                ? 'Failed'
                : job.state === 'done'
                ? 'Done'
                : job.state === 'queued'
                ? typeof job.queue_position !== 'number'
                  ? 'Waiting in queue…'
                  : job.queue_position === 0
                  ? "You're next — starting shortly…"
                  : `${job.queue_position} jobs ahead of you`
                : STAGE_LABELS[job.current_stage] || `State: ${job.state}`
              : 'Starting…'}
          </div>
          <div className="progress">
            <div style={{ width: `${job?.progress_percent ?? 0}%` }} />
          </div>

          {!terminal && (
            <div className="help" style={{ marginTop: '0.75rem' }}>
              Keep this tab open until your video is ready — if you refresh or
              close it, you won't be able to get back to this video.
            </div>
          )}

          {job?.error_message && (
            <div className="error">
              {FRIENDLY_ERRORS[job.error_code] || (
                <><strong>{job.error_code}</strong>: {job.error_message}</>
              )}
            </div>
          )}

          {/* Video player + download once Stage G finishes. */}
          {videoSrc && (
            <div className="result">
              <div className="result-message">Your video is ready.</div>
              <a href={videoSrc} download className="download-link">
                Download MP4
                {typeof job.video_size_bytes === 'number' && (
                  <span className="size">
                    {' '}({(job.video_size_bytes / (1024 * 1024)).toFixed(1)} MB)
                  </span>
                )}
              </a>
              <div className="help" style={{ marginTop: '0.5rem' }}>
                Available for 24 hours.
              </div>
            </div>
          )}

          {/* Debug pane during local development — easier than digging in devtools */}
          {job && (
            <pre className="debug">{JSON.stringify(job, null, 2)}</pre>
          )}

          {terminal && (
            <button onClick={reset}>Start another</button>
          )}
        </div>
      )}
    </div>
  );
}