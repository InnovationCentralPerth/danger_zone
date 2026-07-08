# Prompt for Generating Tech Demo Posters

Use this prompt template in any LLM (like Claude, ChatGPT, or Gemini) to generate a single-page, print-ready HTML poster for your projects.

---

## The Prompt Template

I need a single-page, print-ready HTML/CSS poster that I can hand out at a tech demo for my project called **[Project Name]**.

Here is a summary of what the project does:
**[Briefly describe your project, its main features, and the core technologies used in 2-3 sentences]**

Please generate a single `index.html` file containing all the HTML and CSS. Follow these strict design requirements:

**Layout & Print Constraints:**
1. Set the exact dimensions to A4 size (`width: 210mm; height: 297mm;`).
2. Use a CSS reset and include `@media print` rules to ensure it prints exactly as it looks on screen (no margins, preserve background graphics).
3. Ensure all content fits comfortably on a **single page** without overflowing. Use `flex-grow` or `margin-top: auto` to push the footer to the bottom.

**Design Aesthetic:**
1. Use a clean, modern, high-contrast style that looks professional when printed (e.g., dark text on a white/light gray background).
2. Include a subtle background pattern (like a faint grid or dot matrix) to give it a technical feel.
3. Use standard sans-serif system fonts (`-apple-system, system-ui, sans-serif`).

**Content Structure:**
1. **Header:** Large, bold title and a short, catchy subtitle.
2. **Intro:** A 1-2 sentence high-level summary of the project's value proposition.
3. **Main Content (2-Column Grid):** Create 4 scannable "feature cards" using a 2x2 CSS grid. Each card should have an emoji icon, a bold title, and 2-3 bullet points. 
   *   *Card 1:* How to interact with the demo.
   *   *Card 2:* The core innovation/differentiator.
   *   *Card 3:* Under the hood (architecture).
   *   *Card 4:* [Add a project-specific feature here].
4. **Highlight Section:** A dark, full-width banner summarizing the technical flow or pipeline (e.g., Step A ➔ Step B ➔ Step C).
5. **Footer:** A call-to-action on the left (e.g., "Try the demo by asking...") and small "tech stack" pill badges on the right.

---

### Why this prompt works well:
*   **Exact Dimensions:** It forces the LLM to use `mm` instead of generic percentages, ensuring the CSS layout precisely maps to physical paper.
*   **Forces the Structure:** Left to their own devices, LLMs often create long scrolling websites. Explicitly demanding a header, an intro, a 2x2 grid, a full-width banner, and a footer forces the LLM to organize the information into bite-sized, scannable chunks that fit on one page.
*   **Print-Specific CSS:** It reminds the LLM to include the specific CSS rules needed to prevent browsers from stripping out colors when printing.