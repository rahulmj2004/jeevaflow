import { useEffect, useRef, useState } from "react";

interface Props {
  onCapture: (file: File) => void;
  onClose: () => void;
}

/**
 * Browser camera capture. The captured photo is handed back as a
 * normal JPEG File and uploaded through the same document endpoint
 * (and therefore the same quality gate and OCR pipeline) as any file.
 */
export function CameraCapture({ onCapture, onClose }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let cancelled = false;

    async function start() {
      if (!navigator.mediaDevices?.getUserMedia) {
        setError("Camera access is not available in this browser. Use “Take photo” on a phone instead.");
        return;
      }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } },
          audio: false,
        });
        if (cancelled) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        streamRef.current = stream;
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          await videoRef.current.play().catch(() => undefined);
        }
        setReady(true);
      } catch (err) {
        setError(`Could not open the camera: ${(err as Error).message || "permission denied"}.`);
      }
    }

    start();

    return () => {
      cancelled = true;
      streamRef.current?.getTracks().forEach((track) => track.stop());
    };
  }, []);

  function capture() {
    const video = videoRef.current;
    if (!video || !video.videoWidth) return;
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d")?.drawImage(video, 0, 0);
    canvas.toBlob(
      (blob) => {
        if (!blob) {
          setError("Could not capture the photo.");
          return;
        }
        const stamp = new Date().toISOString().replace(/[:.]/g, "-");
        onCapture(new File([blob], `camera_${stamp}.jpg`, { type: "image/jpeg" }));
      },
      "image/jpeg",
      0.92,
    );
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Capture document photo">
      <div className="modal">
        <div className="modal-head">
          <h3>Photograph a document</h3>
          <button type="button" className="btn btn-small" onClick={onClose}>
            Close
          </button>
        </div>
        {error ? (
          <div className="banner banner-danger">{error}</div>
        ) : (
          <video ref={videoRef} className="camera-video" playsInline muted />
        )}
        <ul className="small muted capture-tips">
          <li>Place the page on a flat surface.</li>
          <li>Use good light and avoid shadows.</li>
          <li>Keep the whole page visible and hold steady.</li>
        </ul>
        <p className="small muted">
          Blurred or dark photos are rejected with a request to retake. Handwritten pages are reviewed by a doctor.
        </p>
        <div className="btn-row">
          <button type="button" className="btn btn-primary" onClick={capture} disabled={!ready || !!error}>
            Capture &amp; upload
          </button>
        </div>
      </div>
    </div>
  );
}
