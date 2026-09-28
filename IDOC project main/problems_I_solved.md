### Problem Coverage
| #   | Problem          | Resolution              |                   |     |
| -----| ------------------| -------------------------| -------------------| -----|
| 1   | Unknown partner  | Human: add WE20 profile | \n"               |     |
| 2   | Blocked material | Human: unblock          | \n"               |     |
| 3   | Low stock        | **Auto: partial fill**  | \n"               |     |
| 4   | Zero stock       | Human: replenish        | \n"               |     |
| 5   | Credit exceeded  | Human: raise limit      | \n"               |     |
| 6   | Unknown customer | Human: add customer     | \n"               |     |
| 7   | Unknown material | Human: add mapping      | \n"               |     |
| 8   | Price mismatch   | Partner: resubmit       | \n"               |     |
| "   | 9                | Past delivery date      | Partner: resubmit | \n" |
| "   | 10               | Zero/neg qty            | Partner: resubmit | \n" |
| "   | 11               | Malformed EDI           | Partner: resubmit | \n" |
| "   | 12               | Duplicate PO            | Auto: suppressed  | \n" |
| "   | 13               | Multiple errors         | Sequential fixes  | \n" |
| "   | 14               | Wrong fix type          | Validation guard  | \n" |
| "   | 15               | DB failure              | Fail-safe error   | \n" |