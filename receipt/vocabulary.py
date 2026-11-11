"""Starter pick-lists for receipt fields stored as text.

These are suggestions, not validation: each list is offered with an "Other" entry, so a lab
can record a value that is not here, and text recorded before the lists existed stays valid.
Container and preservative entries follow the vocabulary of 40 CFR 136.3 Table II and common
commercial chain-of-custody forms; they are not requirements for any particular method.
"""

SEAL_CONDITIONS = ("Intact", "Broken or damaged", "No seal present")

PRESERVATION_CHECKS = (
    "Preservation verified at receipt",
    "Preserved per labels, not verified",
    "No chemical preservation required",
    "Preserved by the laboratory at receipt",
    "Preservation incorrect or missing",
    "Not checked",
)

CONTAINER_TYPES = (
    "40 mL VOA vial",
    "125 mL plastic (HDPE)",
    "250 mL plastic (HDPE)",
    "500 mL plastic (HDPE)",
    "1 L plastic (HDPE)",
    "250 mL amber glass",
    "500 mL amber glass",
    "1 L amber glass",
    "4 oz glass jar",
    "8 oz glass jar",
)

PRESERVATIVES = (
    "None",
    "HNO3 (nitric acid)",
    "H2SO4 (sulfuric acid)",
    "HCl (hydrochloric acid)",
    "NaOH (sodium hydroxide)",
    "Zinc acetate + NaOH",
    "Na2S2O3 (sodium thiosulfate)",
    "NaHSO4 (sodium bisulfate)",
    "Methanol",
    "Ascorbic acid",
)

CONTAINER_CONDITIONS = (
    "Intact",
    "Leaking",
    "Broken or cracked",
    "Lid loose",
    "Headspace or bubbles",
    "Insufficient volume",
    "Label missing or illegible",
)

CONTACT_ROLES = (
    "Report recipient",
    "Billing / accounts payable",
    "Sampler",
    "Project lead",
    "Regulatory contact",
)

DISPOSAL_METHODS = (
    "Returned to client",
    "Neutralized and discharged to sanitary sewer",
    "Hazardous waste contractor",
    "Solid waste (non-hazardous)",
    "Consumed in analysis",
)

US_STATES = (
    ("AL", "Alabama"),
    ("AK", "Alaska"),
    ("AZ", "Arizona"),
    ("AR", "Arkansas"),
    ("CA", "California"),
    ("CO", "Colorado"),
    ("CT", "Connecticut"),
    ("DE", "Delaware"),
    ("DC", "District of Columbia"),
    ("FL", "Florida"),
    ("GA", "Georgia"),
    ("HI", "Hawaii"),
    ("ID", "Idaho"),
    ("IL", "Illinois"),
    ("IN", "Indiana"),
    ("IA", "Iowa"),
    ("KS", "Kansas"),
    ("KY", "Kentucky"),
    ("LA", "Louisiana"),
    ("ME", "Maine"),
    ("MD", "Maryland"),
    ("MA", "Massachusetts"),
    ("MI", "Michigan"),
    ("MN", "Minnesota"),
    ("MS", "Mississippi"),
    ("MO", "Missouri"),
    ("MT", "Montana"),
    ("NE", "Nebraska"),
    ("NV", "Nevada"),
    ("NH", "New Hampshire"),
    ("NJ", "New Jersey"),
    ("NM", "New Mexico"),
    ("NY", "New York"),
    ("NC", "North Carolina"),
    ("ND", "North Dakota"),
    ("OH", "Ohio"),
    ("OK", "Oklahoma"),
    ("OR", "Oregon"),
    ("PA", "Pennsylvania"),
    ("RI", "Rhode Island"),
    ("SC", "South Carolina"),
    ("SD", "South Dakota"),
    ("TN", "Tennessee"),
    ("TX", "Texas"),
    ("UT", "Utah"),
    ("VT", "Vermont"),
    ("VA", "Virginia"),
    ("WA", "Washington"),
    ("WV", "West Virginia"),
    ("WI", "Wisconsin"),
    ("WY", "Wyoming"),
    ("AS", "American Samoa"),
    ("GU", "Guam"),
    ("MP", "Northern Mariana Islands"),
    ("PR", "Puerto Rico"),
    ("VI", "U.S. Virgin Islands"),
)
