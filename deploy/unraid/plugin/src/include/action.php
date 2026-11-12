<?php
/* MCPanel plugin: the endpoint of the page. POST only: Unraid's local_prepend.php refuses every POST without
 * the session's CSRF token before this runs (jQuery's $.post of the web UI adds it). */
require_once __DIR__ . '/common.php';
header('Content-Type: application/json');
header('Cache-Control: no-store');
if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    http_response_code(405);
    echo json_encode(['ok' => false, 'error' => 'POST only']);
    exit;
}
echo json_encode(mcpanel_handle($_POST, mcpanel_config()), JSON_INVALID_UTF8_SUBSTITUTE);
