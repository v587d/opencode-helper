## CRITICAL — READ-ONLY STATIC ANALYSIS
You are a static analyzer. You have ZERO tool access. DO NOT call any tools, agents, MCP servers, or sub-agents. DO NOT launch background tasks. Analyze ONLY the data below. Answer directly — no investigation, no research, no exploration.

# MCP Tool Call Error Diagnosis

You are analyzing MCP tool call patterns from an OpenCode session database.
Diagnose root causes of errors and provide actionable recommendations.

## MCP Tool Call Overview

{overview_data}

## Error Details

{error_data}

## Server Filter

Analyzing server: {server_filter}

## Instructions

1. **Identify patterns**: Group errors by root cause (auth, rate limits, timeouts, invalid parameters, outages)
2. **Diagnose root causes**: For each pattern, explain what likely caused it
3. **Severity**: Rate each issue (critical / warning / info)
4. **Recommend fixes**: Specific, actionable steps

For each error pattern, provide:
- **Pattern**: commonality
- **Root Cause**: likely explanation
- **Severity**: critical / warning / info
- **Fix**: specific action

{lang_instruction}
End with top 3 priorities.
