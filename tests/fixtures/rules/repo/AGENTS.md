# Agent instructions

- **Never** edit generated code in `api/gen/`.
  <!-- ruleproof: demo-paths paths=api/gen/ -->

Run the tests before you finish. <!-- ruleproof: demo-all pattern="py test" flag=yes id=run-tests -->

The syntax is `<!-- ruleproof: <check> key=value -->`, shown here as an example.

```markdown
<!-- ruleproof: not-a-check paths=x -->
```
