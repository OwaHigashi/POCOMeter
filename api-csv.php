<?php
declare(strict_types=1);

const METER_CSV_DOWNLOAD = true;

function downloadCsv(string $day, array $selections): never
{
    header('Content-Type: text/csv; charset=utf-8');
    header('Content-Disposition: attachment; filename="pococha-meter-' . $day . '.csv"');
    header('Access-Control-Expose-Headers: Content-Disposition');
    http_response_code(200);
    if (($_SERVER['REQUEST_METHOD'] ?? 'GET') === 'HEAD') {
        exit;
    }

    $output = fopen('php://output', 'wb');
    fwrite($output, "\xEF\xBB\xBF");
    fputcsv($output, [
        'date', 'rank', 'close', 'border', 'forecast_points', 'actual_points',
        'forecast_method', 'provisional', 'holiday_adjustment_points', 'event_adjustment_points',
        'window_start', 'window_end', 'holiday_names', 'event_names',
        'event_schedule_available', 'missing_event_months',
        'forecast_at', 'observations_at', 'latest_actual_date', 'last_update_attempt_at',
        'update_failed', 'forecast_source', 'actual_source', 'forecast_low', 'forecast_high',
        'rank_adjustment_points', 'holiday_adjustment_percent', 'rank_adjustment_percent',
    ], ',', '"', '', "\r\n");

    foreach ($selections as $selection) {
        $holidayNames = csvList(array_column($selection['holidays'], 'name'));
        $eventNames = csvList(array_column($selection['events']['items'], 'name'));
        $missingMonths = csvList($selection['events']['missingMonths']);
        $eventScheduleAvailable = $selection['events']['scheduleAvailable'];
        $update = $selection['update'];
        foreach (BORDER_LABELS as $border => $label) {
            $basis = $selection['forecast']['basis'][$label] ?? null;
            $provisional = isset($basis['context'])
                || ($basis['weekday']['provisional'] ?? false)
                || ($basis['method'] ?? null) === 'source_median';
            fputcsv($output, [
                $selection['date'],
                $selection['rank'],
                $selection['closingHour'],
                $border,
                $selection['forecast']['points'][$label] ?? null,
                $selection['actual']['points'][$label] ?? null,
                $basis['method'] ?? null,
                $basis === null ? null : (int) $provisional,
                $basis['context']['holiday'] ?? null,
                $eventScheduleAvailable ? ($basis['context']['events'] ?? null) : null,
                $selection['window']['start'],
                $selection['window']['end'],
                $holidayNames,
                $eventNames,
                (int) $eventScheduleAvailable,
                $missingMonths,
                $update['forecastAt'],
                $update['observationsAt'],
                $update['latestActualDate'],
                $update['lastAttemptAt'],
                (int) $update['failed'],
                $basis === null ? null : ($basis['source'] ?? $selection['sources']['observations']),
                $selection['actual']['source'] ?? null,
                $selection['forecast']['range'][$label][0] ?? null,
                $selection['forecast']['range'][$label][1] ?? null,
                $basis['context']['rank'] ?? null,
                $basis['context']['holidayPercent'] ?? null,
                $basis['context']['rankPercent'] ?? null,
            ], ',', '"', '', "\r\n");
        }
    }
    fclose($output);
    exit;
}

function csvList(array $values): string
{
    return json_encode(array_values(array_unique($values)), JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
}

require __DIR__ . '/api.php';
