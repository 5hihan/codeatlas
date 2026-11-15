// CodeAtlas mod: answer a built-in Read or Grep of an indexed source file from the index, in place.
//
// A whole-file Read becomes the file's top-level declarations with line ranges; a Grep for an identifier (through
// the Grep tool or a shell grep/rg in Bash) becomes its definitions and name matches grouped by file. The model gets
// the answer as the tool's result: no denial, no retry, no hook message. Anything the index cannot answer (bounded
// reads, text regexes, other shell commands, unindexed files, no `codeatlas` on PATH) goes ahead unchanged, and the
// PreToolUse guard still applies to it.
const PREFIX = 'mcp__plugin_codeatlas_atlas'

export function register(on, options) {
  on('tool.call', { tool: ['Read', 'Grep', 'Bash'] }, async ($, e, next) => {
    try {
      const { tool, tool_use_id, consent, ...args } = e
      const env = {}
      if (options && options.guard_mode !== undefined) env.CLAUDE_PLUGIN_OPTION_GUARD_MODE = String(options.guard_mode)
      const run = await $.process.run(['codeatlas', 'hook', 'answer', '--prefix', PREFIX],
                                      { stdin: JSON.stringify({ tool_name: tool, tool_input: args }), timeoutMs: 8000, env })
      if (run.exitCode === 0 && run.stdout.trim()) {
        const answer = JSON.parse(run.stdout)
        if (answer && typeof answer.result === 'string' && answer.result) {
          // the engine checks `result` against the tool's own output shape
          const text = answer.result
          const numLines = text.split('\n').length
          if (tool === 'Read') {
            return { result: { type: 'text', file: { filePath: String(args.file_path || ''), content: text, numLines, startLine: 1, totalLines: numLines } } }
          }
          if (tool === 'Bash') {
            return { result: { stdout: text, stderr: '', interrupted: false, isImage: false } }
          }
          const filenames = Array.isArray(answer.files) ? answer.files : []
          return { result: { mode: 'content', content: text, numLines, numMatches: numLines, numFiles: filenames.length, filenames } }
        }
      }
    } catch (err) {
      // fall through: the tool runs as usual
    }
    return next(e)
  })
}
