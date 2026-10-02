import { useEffect, useState } from "react";
import { api } from "../api";

/**
 * Shows one evidence region of the original document.
 *
 * The image comes from a single-use, 60-second token bound to the
 * doctor's session; the server renders it with a watermark (doctor ID
 * and time) and sends Cache-Control: no-store. It is held only as an
 * in-memory blob URL, revoked on close. There is no download link.
 */
export function SecureEvidence({ evidenceId, canView }: { evidenceId: number; canView: boolean }) {
  const [url, setUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setUrl(null);
    setError(null);
  }, [evidenceId]);

  useEffect(
    () => () => {
      if (url) URL.revokeObjectURL(url);
    },
    [url],
  );

  if (!canView) {
    return (
      <p className="small muted secure-note">
        The patient has not shared source documents with you, so the original page cannot be shown.
      </p>
    );
  }

  async function open() {
    setBusy(true);
    setError(null);
    try {
      const blob = await api.evidenceImage(evidenceId);
      setUrl(URL.createObjectURL(blob));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="secure-evidence">
      {!url && (
        <button type="button" className="btn" disabled={busy} onClick={open}>
          {busy ? "Opening securely…" : "View source region"}
        </button>
      )}
      {error && (
        <div className="banner banner-danger" role="alert">
          {error}
        </div>
      )}
      {url && (
        <figure className="secure-figure" onContextMenu={(event) => event.preventDefault()}>
          <img src={url} alt="Source region of the original document, watermarked" draggable={false} />
          <figcaption className="small muted">
            Watermarked with your user ID and the time. Single-use link, not cached, no download.
            <button
              type="button"
              className="link-button small"
              onClick={() => {
                URL.revokeObjectURL(url);
                setUrl(null);
              }}
            >
              Close
            </button>
          </figcaption>
        </figure>
      )}
    </div>
  );
}
