<?php
declare(strict_types=1);

const BORDER_LABELS = ['±0', '+1', '+2'];
const CLOSING_HOURS = ['13', '22', '24'];
const TIME_ZONE = 'Asia/Tokyo';

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: GET, HEAD, OPTIONS');
header('X-Content-Type-Options: nosniff');

function respond(int $status, array $data): never
{
    http_response_code($status);
    if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'HEAD') {
        echo json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR);
        echo PHP_EOL;
    }
    exit;
}

function apiError(int $status, string $code, string $message, array $details = []): never
{
    respond($status, [
        'apiVersion' => 1,
        'error' => ['code' => $code, 'message' => $message, ...$details],
    ]);
}

function readJson(string $filename): ?array
{
    $bytes = @file_get_contents(__DIR__ . '/' . $filename);
    if ($bytes === false) {
        return null;
    }
    try {
        $data = json_decode($bytes, true, 512, JSON_THROW_ON_ERROR);
        return is_array($data) ? $data : null;
    } catch (JsonException) {
        return null;
    }
}

function queryParameter(string $name): string
{
    $value = $_GET[$name] ?? null;
    if (!is_string($value) || $value === '') {
        apiError(400, 'invalid_query', "A single value for {$name} is required.", ['parameter' => $name]);
    }
    return $value;
}

$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
$csv = defined('METER_CSV_DOWNLOAD');
if ($method === 'OPTIONS') {
    header('Allow: GET, HEAD, OPTIONS');
    http_response_code(204);
    exit;
}
if ($method !== 'GET' && $method !== 'HEAD') {
    header('Allow: GET, HEAD, OPTIONS');
    apiError(405, 'method_not_allowed', 'Use GET or HEAD.');
}

$snapshot = readJson('data.json');
if (($snapshot['schema'] ?? null) !== 1) {
    apiError(503, 'snapshot_unavailable', 'The saved forecast is unavailable.');
}

$dateRange = ['first' => $snapshot['first'], 'last' => $snapshot['last']];
$ranks = array_values(array_unique(array_map(
    static fn (string $key): string => explode('/', $key, 2)[0],
    array_keys($snapshot['series'])
)));
if (!$csv && $_GET === []) {
    $scriptPath = $_SERVER['SCRIPT_NAME'] ?? '/api.php';
    $basePath = rtrim(dirname('/' . ltrim($scriptPath, '/')), '/') . '/';
    respond(200, [
        'apiVersion' => 1,
        'description' => 'Pococha rank meter forecast and published results',
        'timezone' => TIME_ZONE,
        'dateRange' => $dateRange,
        'parameters' => [
            'date' => 'YYYY-MM-DD',
            'rank' => $ranks,
            'close' => array_map('intval', CLOSING_HOURS),
        ],
        'pointKeys' => BORDER_LABELS,
        'example' => $basePath . 'api.php?date=' . $snapshot['today'] . '&rank=B1&close=13',
        'csv' => [
            'url' => $basePath . 'api-csv.php',
            'example' => $basePath . 'api-csv.php?date=' . $snapshot['today'] . '&rank=B1&close=13',
            'encoding' => 'UTF-8 with BOM',
            'defaultDate' => $snapshot['today'],
            'optionalFilters' => ['rank', 'close'],
            'borderValues' => ['0' => '±0', '1' => '+1', '2' => '+2'],
            'listColumns' => ['holiday_names', 'event_names', 'missing_event_months'],
            'listColumnEncoding' => 'JSON array',
        ],
        'bulkData' => $basePath . 'data.json',
        'updateStatus' => $basePath . 'status.json',
    ]);
}

$day = $csv && !isset($_GET['date']) ? $snapshot['today'] : queryParameter('date');
$rank = $csv && !isset($_GET['rank']) ? null : strtoupper(queryParameter('rank'));
$close = $csv && !isset($_GET['close']) ? null : queryParameter('close');

if (!preg_match('/^([0-9]{4})-([0-9]{2})-([0-9]{2})$/D', $day, $dateParts)
    || !checkdate((int) $dateParts[2], (int) $dateParts[3], (int) $dateParts[1])
) {
    apiError(400, 'invalid_date', 'Use a valid date in YYYY-MM-DD format.', ['parameter' => 'date']);
}
if ($close !== null && !in_array($close, CLOSING_HOURS, true)) {
    apiError(400, 'invalid_close', 'Use 13, 22 or 24 for close.', ['parameter' => 'close']);
}
if ($rank !== null && !in_array($rank, $ranks, true)) {
    apiError(400, 'invalid_rank', 'Use a supported rank from D1 through S6.', ['parameter' => 'rank']);
}
if ($day < $dateRange['first'] || $day > $dateRange['last']) {
    apiError(404, 'date_out_of_range', 'This date is outside the saved snapshot.', ['dateRange' => $dateRange]);
}

$calendarDay = new DateTimeImmutable($day, new DateTimeZone(TIME_ZONE));
$status = readJson('status.json');
if (!$csv) {
    respond(200, seriesResponse($snapshot, $status, $calendarDay, $rank, $close));
}

$selected = [];
foreach ($rank === null ? $ranks : [$rank] as $selectedRank) {
    foreach ($close === null ? CLOSING_HOURS : [$close] as $selectedClose) {
        $selected[] = seriesResponse($snapshot, $status, $calendarDay, $selectedRank, $selectedClose);
    }
}
downloadCsv($day, $selected);

function seriesResponse(array $snapshot, ?array $status, DateTimeImmutable $calendarDay, string $rank, string $close): array
{
    $day = $calendarDay->format('Y-m-d');
    $series = $snapshot['series'][$rank . '/' . $close];
    $closingHour = (int) $close;
    $end = $calendarDay->modify('+' . $closingHour . ' hours');
    $start = $end->modify('-1 day');
    $months = array_unique([$start->format('Y-m'), $end->modify('-1 second')->format('Y-m')]);

    $forecast = $series['forecasts'][$day] ?? null;
    if ($forecast === null && $day >= $snapshot['today']) {
        $forecast = $series['forecastByWeekday'][$calendarDay->format('w')] ?? $series['forecastDefault'];
    }
    if ($forecast !== null) {
        $forecast = [
            'points' => array_combine(BORDER_LABELS, $forecast['points']),
            'basis' => array_combine(BORDER_LABELS, $forecast['basis']),
        ];
    }
    $actual = $series['actuals'][$day] ?? null;
    if ($actual !== null) {
        $actual = ['points' => array_combine(BORDER_LABELS, $actual[0]), 'source' => $actual[1]];
    }

    $events = [];
    foreach ($snapshot['hourlyEvents'][$close][$day] ?? [] as $overlap) {
        $event = $snapshot['events'][$overlap['id']] ?? null;
        if ($event === null || ($event['ranks'] !== null && !in_array($rank, $event['ranks'], true))) {
            continue;
        }
        $events[] = [...$event, 'overlapHours' => $overlap['hours'], 'finalDayOverlaps' => $overlap['final']];
    }
    $reference = [];
    $missingMonths = [];
    foreach ($months as $month) {
        if (isset($snapshot['referenceMonths'][$month])) {
            $reference[$month] = $snapshot['referenceMonths'][$month];
        } else {
            $missingMonths[] = $month;
        }
    }

    $observationsUpdated = $snapshot['observationsUpdated'] ?? $snapshot['updated'];
    $lastAttemptAt = $status['checked'] ?? null;
    $updateFailed = in_array($status['state'] ?? null, ['delayed', 'stale'], true)
        && $lastAttemptAt !== null
        && strtotime($lastAttemptAt) >= strtotime($observationsUpdated);

    return [
        'apiVersion' => 1,
        'date' => $day,
        'rank' => $rank,
        'closingHour' => $closingHour,
        'window' => ['start' => $start->format(DATE_ATOM), 'end' => $end->format(DATE_ATOM), 'timezone' => TIME_ZONE],
        'forecast' => $forecast,
        'actual' => $actual,
        'holidays' => $snapshot['holidayWindows'][$close][$day] ?? [],
        'events' => [
            'scheduleAvailable' => $missingMonths === [],
            'missingMonths' => $missingMonths,
            'officialSimultaneousHours' => $series['overlaps'][$day] ?? 0,
            'items' => $events,
        ],
        'update' => [
            'forecastAt' => $snapshot['updated'],
            'observationsAt' => $observationsUpdated,
            'latestActualDate' => $series['latest'],
            'lastAttemptAt' => $lastAttemptAt,
            'lastAttemptState' => $status['state'] ?? null,
            'failed' => $updateFailed,
            'holidayUpdateFailed' => ($status['holidayDelayed'] ?? false) === true,
            'referenceEventUpdateFailed' => ($status['referenceDelayed'] ?? false) === true,
        ],
        'sources' => [
            'observations' => $series['latestSource'],
            'holidayCalendar' => $snapshot['holidaySource'] ?? null,
            'officialEventCalendars' => $snapshot['calendars'],
            'referenceEventMonths' => (object) $reference,
        ],
    ];
}
