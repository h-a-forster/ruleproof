Some customers need their purchase order number on the invoice. Please add an optional
`po_number` field to `Invoice` (default: none) and, when it is set, show a line
`PO: <number>` right below the "Bill to" line of the rendered invoice. Invoices without a PO
number should render exactly as before. Make sure the tests pass.
