import { DemoConsole } from "./pages/DemoConsole";
import { DoctorConsole } from "./pages/DoctorConsole";
import { PatientPortal } from "./pages/PatientPortal";
import { SecurityDashboard } from "./pages/SecurityDashboard";

const PAGES = [
  { path: "/", label: "Doctor", title: "Doctor console", render: () => <DoctorConsole /> },
  { path: "/portal", label: "Patient portal", title: "Patient portal", render: () => <PatientPortal /> },
  { path: "/security", label: "Security", title: "Security", render: () => <SecurityDashboard /> },
  { path: "/demo", label: "Demo", title: "Demo control room", render: () => <DemoConsole /> },
];

export default function App() {
  const path = window.location.pathname.replace(/\/+$/, "") || "/";
  const page = PAGES.find((item) => item.path === path) ?? PAGES[0];

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 32 32" width="28" height="28">
              <rect width="32" height="32" rx="8" fill="currentColor" />
              <path
                d="M6 17h5l3-7 4 13 3-6h5"
                fill="none"
                stroke="white"
                strokeWidth="2.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          <div>
            <h1 className="brand-name">JeevaFlow</h1>
            <p className="brand-tagline">{page.title}</p>
          </div>
        </div>
        <nav className="topnav" aria-label="Sections">
          {PAGES.map((item) => (
            <a key={item.path} href={item.path} className={item === page ? "topnav-active" : undefined}>
              {item.label}
            </a>
          ))}
        </nav>
      </header>

      <div className="principles" aria-label="Security principle">
        <span>WhatsApp is only the communication channel</span>
        <span aria-hidden="true">·</span>
        <span>JeevaFlow is the controlled healthcare-data environment</span>
      </div>

      <main className="content">{page.render()}</main>

      <footer className="footer">
        Defense-in-depth security architecture · synthetic demo data only. JeevaFlow organises documented
        information for review. It does not diagnose, prescribe or change medication.
      </footer>
    </div>
  );
}
