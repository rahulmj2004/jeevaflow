import { formatDate, formatDateTime } from "../format";
import type { JourneyDocument } from "../types";
import { EmptyState, SourceIcon, StatusBadge } from "./StatusBadge";

/**
 * Shared documents (metadata only). Originals are never offered as
 * downloads; evidence regions are viewed through the secure viewer.
 */
export function DocumentsList({ documents }: { documents: JourneyDocument[] }) {
  if (documents.length === 0) return <EmptyState title="No documents shared yet" />;

  return (
    <ul className="docs">
      {documents.map((doc) => (
        <li key={doc.ref} className="doc">
          <div className="doc-main">
            <span className="doc-name">{doc.label}</span>
            <span className="small muted">
              <SourceIcon source={doc.source} /> Report {formatDate(doc.document_date)} · received{" "}
              {formatDateTime(doc.created_at)}
            </span>
          </div>
          <div className="doc-badges">
            <StatusBadge status={doc.status} />
            {doc.quality !== "GOOD" && <StatusBadge status={doc.quality} />}
          </div>
        </li>
      ))}
    </ul>
  );
}
