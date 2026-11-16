export default function Placeholder({ title }: { title: string }) {
  return (
    <>
      <div className="heading">
        <div>
          <h1>{title}</h1>
          <p>Module scaffolded in navigation · full build per phased roadmap</p>
        </div>
        <span className="demo">COMING NEXT</span>
      </div>
      <div className="panel" style={{ padding: 20, color: "var(--muted)", lineHeight: 1.6 }}>
        This screen matches the product IA from the YesWeigh CRM prototype. Implementation continues
        phase-by-phase (contacts, WhatsApp inbox, campaigns, automation, Meta, quotations, service, AI).
        See <code>DEVELOPMENT_STATUS.md</code> and <code>PROJECT_CONTEXT.md</code>.
      </div>
    </>
  );
}
