<?php
/* The endpoint of the Unraid plugin's page (deploy/unraid/plugin/src/include/{common,action}.php) with PHP's CLI
 * against a stand-in for bin/mcpanel. Run by tests/test_deploy_unraid_plugin_php.py and by CI:
 *   php backend/tests/unraid_plugin_php/endpoint_test.php deploy/unraid/plugin/src
 * Prints "endpoint: N checks ok" or the first failure (exit 1). */
declare(strict_types=1);

$src = realpath($argv[1] ?? '') ?: exit("usage: php endpoint_test.php <plugin src folder>\n");
$work = sys_get_temp_dir() . '/mcpanel-endpoint-' . getmypid();
$plugin = "$work/plugin";
@mkdir("$plugin/include", 0755, true);
@mkdir("$plugin/bin", 0755, true);
copy("$src/include/common.php", "$plugin/include/common.php");
copy("$src/include/action.php", "$plugin/include/action.php");
// the stand-in runner: writes its arguments down, one per line, and answers like the real one
file_put_contents("$plugin/bin/mcpanel", <<<'SH'
#!/bin/sh
here=$(dirname "$0")
: >"$here/args"
for a in "$@"; do printf 'ARG:%s\n' "$a" >>"$here/args"; done
case "$1" in
    facts) printf 'PLUGIN_VERSION=2026.11.12\nLOG_FILE=b.log\nLOG_FILE=a.log\nUSER_SCRIPT=one\nnot a fact\nlower=x\nBUSY=0\n' ;;
    detect) printf 'DETECTED=mcpanel|/mnt/user/appdata/mcpanel-src\n' ;;
    start) if [ -e "$here/busy" ]; then echo "BUSY: another action is running"; exit 75; fi; echo "STARTED: $2" ;;
    describe) echo "Runs as root with umask 022, in this order:"; echo "bash x $2" ;;
    save) echo "SAVED: x" ;;
esac
SH);
chmod("$plugin/bin/mcpanel", 0755);
require "$plugin/include/common.php";

$checks = 0;
function check(bool $ok, string $what): void
{
    global $checks;
    $checks++;
    if (!$ok) {
        fwrite(STDERR, "FAILED: $what\n");
        exit(1);
    }
}
function args(): array
{
    global $plugin;
    return array_map(fn($l) => substr($l, 4), file("$plugin/bin/args", FILE_IGNORE_NEW_LINES));
}

$run = "$work/run";
@mkdir("$run/logs", 0755, true);
$cfg = ['runner' => "$plugin/bin/mcpanel", 'run_dir' => $run];

// facts: lists for the keys that repeat, nothing that is not KEY=value
$r = mcpanel_handle(['action' => 'facts'], $cfg);
check($r['ok'] && $r['facts']['PLUGIN_VERSION'] === '2026.11.12', 'facts');
check($r['facts']['LOG_FILE'] === ['b.log', 'a.log'] && $r['facts']['USER_SCRIPT'] === ['one'], 'fact lists');
check(!isset($r['facts']['lower']) && count($r['facts']) === 6, 'only KEY=value lines');
check(args() === ['facts'], 'facts: one argument');

// describe and start: the task and its one option, as separate arguments, nothing else
$r = mcpanel_handle(['action' => 'describe', 'task' => 'update-run', 'option' => 'with-migrations'], $cfg);
check($r['ok'] && args() === ['describe', 'update-run', 'with-migrations'], 'describe with the option');
$r = mcpanel_handle(['action' => 'start', 'task' => 'status', 'option' => ''], $cfg);
check($r['ok'] && $r['text'] === 'STARTED: status' && args() === ['start', 'status'], 'start without an option');
$r = mcpanel_handle(['action' => 'start', 'task' => 'status'], $cfg);
check($r['ok'] && args() === ['start', 'status'], 'a missing option is none');
touch("$plugin/bin/busy");
$r = mcpanel_handle(['action' => 'start', 'task' => 'status'], $cfg);
check(!$r['ok'] && $r['busy'] === true, 'busy');
unlink("$plugin/bin/busy");

unlink("$plugin/bin/args");
foreach ([
    ['action' => 'start', 'task' => 'nonsense'],
    ['action' => 'start', 'task' => 'status', 'option' => 'with-migrations'],
    ['action' => 'start', 'task' => 'update-run', 'option' => '--with-migrations'],
    ['action' => 'start', 'task' => 'rollback-run', 'option' => 'with-migrations'],
    ['action' => 'start', 'task' => 'status; id'],
    ['action' => 'start', 'task' => ['status']],
    ['action' => 'start', 'task' => "status\n"],
    ['action' => 'describe', 'task' => 'update-run', 'option' => 'with-migrations x'],
    ['action' => 'start'],
    ['action' => 'rm'],
    ['action' => ['facts']],
    [],
] as $post) {
    $r = mcpanel_handle($post, $cfg);
    check($r['ok'] === false, 'refused: ' . json_encode($post));
}
check(!file_exists("$plugin/bin/args"), 'nothing refused reached the runner');

// save: three values, trimmed, as three arguments (the runner checks them)
$r = mcpanel_handle(['action' => 'save', 'checkout' => ' /mnt/user/appdata/mcpanel-src ', 'project' => 'mcpanel', 'compose_bin' => ''], $cfg);
check($r['ok'] && args() === ['save', '/mnt/user/appdata/mcpanel-src', 'mcpanel', ''], 'save');
$r = mcpanel_handle(['action' => 'save', 'checkout' => "/x\n/y", 'project' => '', 'compose_bin' => ''], $cfg);
check(!$r['ok'] && args() === ['save', '/mnt/user/appdata/mcpanel-src', 'mcpanel', ''], 'save refuses a line break');
$r = mcpanel_handle(['action' => 'save', 'checkout' => str_repeat('a', 2000), 'project' => '', 'compose_bin' => ''], $cfg);
check(!$r['ok'], 'save refuses a long value');

// the log: the current task's, or one by its name; never anything else
file_put_contents("$run/logs/20261112-101500-status.log", "line one\nline two\n");
file_put_contents("$run/task.env", "TASK=status\nPID=1\nLOG=$run/logs/20261112-101500-status.log\nRESULT=ok\n");
$r = mcpanel_handle(['action' => 'log', 'name' => '', 'offset' => '0'], $cfg);
check($r['ok'] && $r['data'] === "line one\nline two\n" && $r['offset'] === 18 && $r['current'], 'the current log');
check($r['task']['RESULT'] === 'ok' && $r['busy'] === false, 'its task');
$r = mcpanel_handle(['action' => 'log', 'name' => '20261112-101500-status.log', 'offset' => '9'], $cfg);
check($r['data'] === "line two\n" && $r['offset'] === 18, 'from an offset');
$r = mcpanel_handle(['action' => 'log', 'name' => '20261112-101500-status.log', 'offset' => '999'], $cfg);
check($r['data'] === '' && $r['offset'] === 18, 'past the end');
foreach (['../task.env', '/etc/passwd', 'x.log', '20261112-101500-status.log/..', '20261112-101500-STATUS.log'] as $name) {
    $r = mcpanel_handle(['action' => 'log', 'name' => $name, 'offset' => '0'], $cfg);
    check($r['ok'] && $r['data'] === '' && $r['name'] === '', "log name refused: $name");
}
foreach (['-1', '1e3', 'x'] as $offset) {
    $r = mcpanel_handle(['action' => 'log', 'name' => '', 'offset' => $offset], $cfg);
    check($r['ok'] === false, "offset refused: $offset");
}

// busy: the lock is held (flock, as bin/mcpanel and its children hold it)
check(mcpanel_busy($cfg) === false, 'no lock file: not busy');
$fh = fopen("$run/lock", 'c');
check(mcpanel_busy($cfg) === false, 'a free lock: not busy');
flock($fh, LOCK_EX);
check(mcpanel_busy($cfg) === true, 'a held lock: busy');
flock($fh, LOCK_UN);
check(mcpanel_busy($cfg) === false, 'released: not busy');

// the run folder is read only when it is a folder of this user's that group and others cannot write (F-12)
check(mcpanel_run_dir_ok($cfg), 'the run folder is ours');
chmod($run, 0777);
$r = mcpanel_handle(['action' => 'log', 'name' => '', 'offset' => '0'], $cfg);
check($r['ok'] === false && str_contains($r['error'], 'not root'), 'a run folder others can write is not read');
$fh = fopen("$run/lock", 'c');
flock($fh, LOCK_EX);
check(mcpanel_busy($cfg) === false, 'nor its lock');
flock($fh, LOCK_UN);
chmod($run, 0755);
chmod("$run/logs", 0777);
check(!mcpanel_run_dir_ok($cfg), 'nor a logs folder others can write');
chmod("$run/logs", 0755);
rename($run, "$work/run-real");
symlink("$work/run-real", $run);
check(!mcpanel_run_dir_ok($cfg), 'nor a link');
unlink($run);
rename("$work/run-real", $run);
check(mcpanel_run_dir_ok(['run_dir' => "$work/none"]), 'a run folder that is not there yet is fine');
check(mcpanel_config()['run_dir'] === '/run/mcpanel', 'the real run folder is /run/mcpanel');

// action.php: POST only; it runs bin/mcpanel next to its folder
$_SERVER['REQUEST_METHOD'] = 'GET';
$out = shell_exec(PHP_BINARY . ' -r ' . escapeshellarg('$_SERVER["REQUEST_METHOD"]="GET"; include "' . "$plugin/include/action.php" . '";'));
check(json_decode($out, true) === ['ok' => false, 'error' => 'POST only'], 'GET is refused');
$out = shell_exec(PHP_BINARY . ' -r ' . escapeshellarg('$_SERVER["REQUEST_METHOD"]="POST"; $_POST=["action"=>"detect"]; include "' . "$plugin/include/action.php" . '";'));
$r = json_decode($out, true);
check($r['ok'] === true && $r['facts']['DETECTED'] === ['mcpanel|/mnt/user/appdata/mcpanel-src'], 'action.php runs the runner of its plugin');

shell_exec('rm -rf ' . escapeshellarg($work));
echo "endpoint: $checks checks ok\n";
