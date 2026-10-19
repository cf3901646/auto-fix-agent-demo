# Auto-Fix PR Agent (OpenRouter + GitHub Actions)

Autonomous GitHub bug auto-fixer and pull request creator powered by OpenRouter AI and GitHub Actions.

## How It Works

1. Open an Issue describing the bug (mentioning the affected file or stack trace).
2. Add the `auto-fix` label to the Issue, or comment `/fix`.
3. GitHub Actions triggers automatically in the cloud:
   - Fetches the Issue details
   - Inspects the affected source code
   - Calls OpenRouter AI to formulate a surgical search-and-replace fix
   - Runs unit tests
   - Creates a new branch, commits the fix, and opens a Pull Request linked to the Issue (`Closes #<ID>`).
