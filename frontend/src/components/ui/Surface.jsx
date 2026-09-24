export function Surface({ as: Component = 'section', className = '', children, ...props }) {
  return <Component className={`surface ${className}`.trim()} {...props}>{children}</Component>;
}

export function SectionTitle({ icon, eyebrow, title, aside }) {
  return <div className="section-title">
    <div className="section-title-copy">
      <span className="section-icon">{icon}</span>
      <div>{eyebrow && <p className="section-eyebrow">{eyebrow}</p>}<h2>{title}</h2></div>
    </div>
    {aside}
  </div>;
}
