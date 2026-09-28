## Development Workflow — Maker-Checker


Every code change MUST go through this workflow:


1. **Understand** — Read the requirements. Ask clarifying questions if needed.

2. **Make** — Use @maker to implement the changes.

3. **Check** — Use @checker to review what the Maker wrote.

4. **Fix** — If the Checker found Critical or Important issues, use @maker

   to fix them.

5. **Re-check** — Run @checker again after fixes. Repeat until the verdict

   is APPROVE.

6. **Commit** — Only after the Checker approves.


Never skip the Checker step. Never let the Maker review its own code.

