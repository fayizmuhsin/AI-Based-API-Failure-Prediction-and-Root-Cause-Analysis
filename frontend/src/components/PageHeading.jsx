export default function PageHeading({ eyebrow, title, description, children }) {
  return (
    <div className="page-heading">
      <div><p className="eyebrow">{eyebrow}</p><h2>{title}</h2><p className="page-description">{description}</p></div>
      {children && <div className="page-heading-action">{children}</div>}
    </div>
  )
}
