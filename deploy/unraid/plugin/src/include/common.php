<?php
/* MCPanel plugin for Unraid: what the page's endpoint (action.php) does. Every action ends in bin/mcpanel,
 * started without a shell (proc_open with an argument list); the runner checks everything again. Unraid's
 * local_prepend.php has already refused a POST without the right CSRF token before this file runs.
 *
 * NOT RUN ON A REAL UNRAID HOST YET (docs/UNRAID.md, "The Unraid plugin").
 */

const MCPANEL_TASKS = [
    'status' => [''],
    'check-updates' => [''],
    'update-prepare' => [''],
    'update-run' => ['', 'with-migrations'],
    'rollback-prepare' => [''],
    'rollback-run' => ['', 'archive-other-games'],
    'boot-status' => [''],
    'boot-dry-run' => [''],
    'boot-run' => [''],
    'firewall-status' => [''],
    'firewall-dry-run' => [''],
    'firewall-install' => [''],
    'firewall-remove' => [''],
    'compose-install' => [''],
];
const MCPANEL_LOG_CHUNK = 262144;

function mcpanel_config(): array
{
    return [
        'runner' => dirname(__DIR__) . '/bin/mcpanel',
        'run_dir' => '/tmp/mcpanel',
    ];
}

/* Run the runner with these arguments (no shell). Returns [exit code, standard output + error]. */
function mcpanel_run(array $cfg, array $args): array
{
    umask(022);
    $pipes = [];
    $proc = proc_open(
        array_merge([$cfg['runner']], $args),
        [0 => ['file', '/dev/null', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
        $pipes
    );
    if (!is_resource($proc)) {
        return [127, 'could not start ' . $cfg['runner']];
    }
    $out = stream_get_contents($pipes[1]);
    $err = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    $rc = proc_close($proc);
    return [$rc, rtrim($out . $err)];
}

/* KEY=value lines of `mcpanel facts` (keys that come more than once become lists). */
function mcpanel_parse_facts(string $text): array
{
    $lists = ['LOG_FILE', 'UPDATE_COMMIT', 'USER_SCRIPT', 'DETECTED'];
    $facts = array_fill_keys($lists, []);
    foreach (explode("\n", $text) as $line) {
        if (!preg_match('/^([A-Z][A-Z_]*)=(.*)$/', $line, $m)) {
            continue;
        }
        if (in_array($m[1], $lists, true)) {
            $facts[$m[1]][] = $m[2];
        } else {
            $facts[$m[1]] = $m[2];
        }
    }
    return $facts;
}

/* A value of the request: a string of at most $max characters without control characters, else null. */
function mcpanel_field(array $post, string $key, int $max = 1024): ?string
{
    $value = $post[$key] ?? '';
    if (!is_string($value) || strlen($value) > $max || preg_match('/[\x00-\x1f\x7f]/', $value)) {
        return null;
    }
    return $value;
}

function mcpanel_task(array $post): ?array
{
    $task = mcpanel_field($post, 'task', 64);
    $option = mcpanel_field($post, 'option', 64);
    if ($task === null || $option === null || !array_key_exists($task, MCPANEL_TASKS)) {
        return null;
    }
    if (!in_array($option, MCPANEL_TASKS[$task], true)) {
        return null;
    }
    return $option === '' ? [$task] : [$task, $option];
}

/* The log of a task: only files the runner wrote (logs/<time>-<task>.log), read from an offset. */
function mcpanel_log(array $cfg, array $post): array
{
    $logs = $cfg['run_dir'] . '/logs';
    $name = mcpanel_field($post, 'name', 128);
    $offset = mcpanel_field($post, 'offset', 12);
    if ($name === null || $offset === null || ($offset !== '' && !ctype_digit($offset))) {
        return ['ok' => false, 'error' => 'bad request'];
    }
    $task = [];
    if (is_file($cfg['run_dir'] . '/task.env')) {
        foreach (file($cfg['run_dir'] . '/task.env', FILE_IGNORE_NEW_LINES) as $line) {
            if (preg_match('/^(TASK|OPTION|PID|STARTED|LOG|RESULT|EXIT|ENDED)=(.*)$/', $line, $m)) {
                $task[$m[1]] = $m[2];
            }
        }
    }
    if ($name === '') {
        $name = basename($task['LOG'] ?? '');
    }
    if (!preg_match('/^[0-9]{8}-[0-9]{6}-[a-z-]{2,40}\.log$/', $name)) {
        return ['ok' => true, 'name' => '', 'data' => '', 'offset' => 0, 'busy' => mcpanel_busy($cfg), 'task' => $task];
    }
    $path = "$logs/$name";
    $size = is_file($path) ? filesize($path) : 0;
    $from = $offset === '' ? 0 : min((int)$offset, $size);
    $data = '';
    if ($size > $from && ($fh = fopen($path, 'rb'))) {
        fseek($fh, $from);
        $data = fread($fh, MCPANEL_LOG_CHUNK);
        fclose($fh);
    }
    return [
        'ok' => true,
        'name' => $name,
        'data' => $data,
        'offset' => $from + strlen($data),
        'size' => $size,
        'busy' => mcpanel_busy($cfg),
        'current' => basename($task['LOG'] ?? '') === $name,
        'task' => $task,
    ];
}

/* A task runs while its lock is held (the kernel frees it when the task and its children are gone). */
function mcpanel_busy(array $cfg): bool
{
    $lock = $cfg['run_dir'] . '/lock';
    if (!is_file($lock) || !($fh = @fopen($lock, 'r'))) {
        return false;
    }
    $free = flock($fh, LOCK_SH | LOCK_NB);
    if ($free) {
        flock($fh, LOCK_UN);
    }
    fclose($fh);
    return !$free;
}

function mcpanel_handle(array $post, array $cfg): array
{
    $action = mcpanel_field($post, 'action', 32);
    switch ($action) {
        case 'facts':
            [$rc, $out] = mcpanel_run($cfg, ['facts']);
            return ['ok' => $rc === 0, 'facts' => mcpanel_parse_facts($out)];
        case 'detect':
            [$rc, $out] = mcpanel_run($cfg, ['detect']);
            return ['ok' => $rc === 0, 'facts' => mcpanel_parse_facts($out)];
        case 'describe':
        case 'start':
            $args = mcpanel_task($post);
            if ($args === null) {
                return ['ok' => false, 'error' => 'not a task of this page'];
            }
            [$rc, $out] = mcpanel_run($cfg, array_merge([$action], $args));
            return ['ok' => $rc === 0, 'busy' => $rc === 75, 'text' => $out];
        case 'save':
            $values = [];
            foreach (['checkout', 'project', 'compose_bin'] as $key) {
                $value = mcpanel_field($post, $key);
                if ($value === null) {
                    return ['ok' => false, 'error' => "$key: not a value the settings take"];
                }
                $values[] = trim($value);
            }
            [$rc, $out] = mcpanel_run($cfg, array_merge(['save'], $values));
            return ['ok' => $rc === 0, 'text' => $out];
        case 'log':
            return mcpanel_log($cfg, $post);
    }
    return ['ok' => false, 'error' => 'unknown action'];
}
