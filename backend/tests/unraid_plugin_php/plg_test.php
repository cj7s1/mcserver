<?php
/* mcpanel.plg read the way Unraid's plugin manager reads it (dynamix.plugin.manager/scripts/plugin:
 * simplexml_load_file with LIBXML_NOCDATA; an INLINE file is trim(INLINE) . PHP_EOL; Run FILEs in order).
 *   php backend/tests/unraid_plugin_php/plg_test.php deploy/unraid/plugin
 * Prints "plg: N files as the sources" or the first difference (exit 1). */
declare(strict_types=1);

$dir = realpath($argv[1] ?? '') ?: exit("usage: php plg_test.php <deploy/unraid/plugin>\n");
$xml = @simplexml_load_file("$dir/mcpanel.plg", null, LIBXML_NOCDATA);
if ($xml === false) {
    fwrite(STDERR, "FAILED: the plugin manager could not parse mcpanel.plg\n");
    exit(1);
}
function fail(string $what): never
{
    fwrite(STDERR, "FAILED: $what\n");
    exit(1);
}
$attrs = $xml->attributes();
foreach (['name' => 'mcpanel', 'min' => '7.0.0', 'launch' => 'Settings/MCPanel'] as $key => $want) {
    if ((string)$attrs->$key !== $want) fail("PLUGIN $key is '{$attrs->$key}'");
}
if (!preg_match('/^\d{4}\.\d{2}\.\d{2}[a-z]?$/', (string)$attrs->version)) fail('version');
if (!str_ends_with((string)$attrs->pluginURL, '/deploy/unraid/plugin/mcpanel.plg')) fail('pluginURL');
$prefix = '/usr/local/emhttp/plugins/mcpanel/';
$files = 0;
$runs = [];
foreach ($xml->FILE as $file) {
    $name = (string)($file->attributes()->Name ?? '');
    $method = (string)($file->attributes()->Method ?? 'install');
    if ($name === '') {
        $runs[] = $method . ':' . strtok(trim((string)$file->INLINE), "\n");
        continue;
    }
    if (!str_starts_with($name, $prefix)) fail("a file outside the plugin's folder: $name");
    $contents = trim((string)$file->INLINE) . PHP_EOL;
    if ($contents !== file_get_contents("$dir/src/" . substr($name, strlen($prefix)))) fail("$name differs from its source");
    if (!preg_match('/^0[67][45][45]$/', (string)$file->attributes()->Mode)) fail("$name has no Mode");
    $files++;
}
if ($runs !== ['install:#!/bin/bash', 'install:#!/bin/bash', 'remove:#!/bin/bash']) fail('the Run scripts: ' . json_encode($runs));
if (trim((string)$xml->CHANGES) === '') fail('CHANGES');
echo "plg: $files files as the sources\n";
