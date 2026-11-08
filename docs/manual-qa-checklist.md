# Isocode Tours — Manual Browser QA Checklist

Use a synthetic enquiry and explicitly provisioned test staff accounts. Do not
use real traveller contact details. This checklist is not evidence that manual
QA has been completed.

## Public enquiry

- [ ] Open a published package detail page on desktop and mobile widths.
- [ ] Submit valid synthetic details with email only; confirm the browser shows
  the received-enquiry confirmation and does not claim email/SMS/WhatsApp delivery.
- [ ] Confirm required name, contact method, date range, adult count, notice
  acknowledgement, and message validation errors are clear and field-specific.
- [ ] Confirm WhatsApp consent starts unchecked and is not inferred from a phone number.
- [ ] Submit with missing/expired/reused nonce, a populated honeypot, and a
  cross-origin request; confirm rejection and no duplicate enquiry.
- [ ] Confirm the stored original message retains its submitted whitespace and
  content; structured fields remain separate.

## Staff review and recommendations

- [ ] Sign in as admin and open the new enquiry. Check private/no-store headers
  and that the original message is shown beside extraction fields.
- [ ] Confirm extraction status, provider/model attribution, and safe failure
  guidance when relevant.
- [ ] Correct an extraction value and accept it. Confirm accepted values are
  distinct from raw AI extraction and a review event records actor/time.
- [ ] Confirm package recommendations use published packages only and show
  verified current prices, basis, season, and staff-confirmation caveats.
- [ ] Confirm provisional recommendations are visibly labelled before acceptance.
- [ ] Sign in as an assigned consultant and verify access; use an unassigned
  consultant account to verify the enquiry is not visible or actionable.

## Reply workflow

- [ ] Generate an AI reply draft with Groq configured. Confirm it is editable,
  attributed to the current staff user, and linked to its source suggestion.
- [ ] Verify database-backed recommendation and price sections, provisional
  warning where applicable, GST confirmation caveat, and no group-total claim.
- [ ] Edit the draft and save. Confirm author, content, and draft state update.
- [ ] Verify provider failure leaves the enquiry, extraction, and existing replies
  unchanged; use the manual reply form as fallback.
- [ ] Sign in as editor/consultant and confirm they cannot approve. As admin,
  approve the draft and confirm the UI says approval does not send it.
- [ ] Open the manual-send confirmation and read the notice. Cancel without
  checking the confirmation box; verify no state change.
- [ ] Only in a controlled synthetic test, after actually sending through an
  approved external channel outside this application, confirm manual-sent
  recordkeeping. Verify actor/time and immutable content. Do not perform an
  external send just to complete a demo checklist.

## Responsive and privacy checks

- [ ] Review public package form and staff enquiry workspace at narrow mobile
  width; check labels, focus order, buttons, tables/cards, and text wrapping.
- [ ] Confirm consultants cannot access another consultant's enquiry by direct URL.
- [ ] Confirm public package pages and sitemap contain no enquiry notes, replies,
  extraction review history, or private recommendation data.
- [ ] Confirm staff pages are not indexed/cached and no external message is sent
  automatically.
