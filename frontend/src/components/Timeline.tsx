import { formatDate, formatValue } from "../format";
import type { TimelineItem } from "../types";
import { EmptyState, SourceIcon, StatusBadge } from "./StatusBadge";

interface Props {
  items: TimelineItem[];
  selectedKey: string | null;
  onSelect: (item: TimelineItem) => void;
}

export function timelineKey(item: TimelineItem): string {
  return `${item.type}-${item.id}`;
}

function groupByDate(items: TimelineItem[]) {
  const groups = new Map<string, TimelineItem[]>();
  for (const item of items) {
    const list = groups.get(item.date) ?? [];
    list.push(item);
    groups.set(item.date, list);
  }
  return [...groups.entries()];
}

function TimelineEntry({ item, selected, onSelect }: { item: TimelineItem; selected: boolean; onSelect: () => void }) {
  if (item.type === "DOCUMENT") {
    return (
      <li className="tl-item tl-document">
        <span className="tl-marker" aria-hidden="true" />
        <div className="tl-doc">
          <span className="tl-kind">Document received</span>
          <span className="tl-doc-name">{item.value}</span>
          <SourceIcon source={item.source} />
          {item.processing_status !== "PROCESSED" && <StatusBadge status={item.processing_status} />}
          {item.quality_status && item.quality_status !== "GOOD" && <StatusBadge status={item.quality_status} />}
        </div>
      </li>
    );
  }

  const isObservation = item.type === "OBSERVATION";

  return (
    <li className={`tl-item ${isObservation ? "tl-observation" : "tl-instruction"}`}>
      <span className="tl-marker" aria-hidden="true" />
      <button
        type="button"
        className={`tl-card ${selected ? "tl-selected" : ""}`}
        onClick={onSelect}
        aria-pressed={selected}
        disabled={!item.evidence}
      >
        <div className="tl-card-top">
          <span className="tl-kind">{isObservation ? item.title : "Doctor instruction"}</span>
          {isObservation ? <StatusBadge status={item.review_status} /> : <StatusBadge status={item.state} />}
        </div>
        <div className={isObservation ? "tl-value" : "tl-instruction-text"}>
          {isObservation ? formatValue(item.value, item.unit) : item.value}
        </div>
        {!isObservation && item.due_date && <div className="tl-due">Due {formatDate(item.due_date)}</div>}
        {item.evidence ? (
          <div className="tl-source">
            <span className="evidence-chip">Evidence #{item.evidence.id}</span>
            <span className="muted">
              {item.evidence.document_filename} · p.{item.evidence.page_number ?? "—"}
            </span>
          </div>
        ) : (
          <div className="tl-source muted">No source evidence</div>
        )}
      </button>
    </li>
  );
}

export function Timeline({ items, selectedKey, onSelect }: Props) {
  if (items.length === 0) {
    return (
      <EmptyState title="No health journey yet">
        Upload a report or process the synthetic demo document to start the timeline.
      </EmptyState>
    );
  }

  return (
    <div className="timeline">
      {groupByDate(items).map(([date, entries]) => (
        <section key={date} className="tl-group">
          <h3 className="tl-date">{formatDate(date)}</h3>
          <ol className="tl-list">
            {entries.map((item) => (
              <TimelineEntry
                key={timelineKey(item)}
                item={item}
                selected={selectedKey === timelineKey(item)}
                onSelect={() => onSelect(item)}
              />
            ))}
          </ol>
        </section>
      ))}
    </div>
  );
}
