(() => {
  "use strict";

  const dateElement = document.getElementById("nextFuturesSettlementDate");
  const countdownElement = document.getElementById("nextFuturesSettlementCountdown");
  if (!dateElement || !countdownElement) return;

  const taipeiToday = () => {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Taipei",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date());
    const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return new Date(Date.UTC(Number(value.year), Number(value.month) - 1, Number(value.day)));
  };

  const thirdWednesday = (year, monthIndex) => {
    const first = new Date(Date.UTC(year, monthIndex, 1));
    const offset = (3 - first.getUTCDay() + 7) % 7;
    return new Date(Date.UTC(year, monthIndex, 1 + offset + 14));
  };

  const today = taipeiToday();
  let settlement = thirdWednesday(today.getUTCFullYear(), today.getUTCMonth());
  if (settlement < today) {
    settlement = thirdWednesday(today.getUTCFullYear(), today.getUTCMonth() + 1);
  }

  const isoDate = settlement.toISOString().slice(0, 10);
  const displayDate = new Intl.DateTimeFormat("zh-TW", {
    timeZone: "UTC",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    weekday: "short",
  }).format(settlement);
  const remainingDays = Math.round((settlement - today) / 86400000);

  dateElement.dateTime = isoDate;
  dateElement.textContent = displayDate;
  countdownElement.textContent = remainingDays === 0 ? "今天結算" : `還剩 ${remainingDays} 天`;

  const holidayDateElement = document.getElementById("nextMarketHolidayDate");
  const holidayCountdownElement = document.getElementById("nextMarketHolidayCountdown");
  if (!holidayDateElement || !holidayCountdownElement) return;

  const renderNextMarketHoliday = async () => {
    try {
      const response = await fetch("./data/calendar/twse_trading_calendar.json", { cache: "no-cache" });
      if (!response.ok) throw new Error(`calendar status ${response.status}`);
      const calendar = await response.json();
      const closedDates = Object.values(calendar.years || {})
        .flatMap((year) => Array.isArray(year.closed_dates) ? year.closed_dates : [])
        .map((value) => new Date(`${value}T00:00:00Z`))
        .filter((value) => !Number.isNaN(value.getTime()))
        .filter((value) => value.getUTCDay() !== 0 && value.getUTCDay() !== 6)
        .filter((value) => value >= today)
        .sort((left, right) => left - right);

      const nextHoliday = closedDates[0];
      if (!nextHoliday) {
        holidayDateElement.textContent = "尚無後續資料";
        holidayCountdownElement.textContent = "等待官方行事曆更新";
        return;
      }

      const holidayIsoDate = nextHoliday.toISOString().slice(0, 10);
      const holidayDisplayDate = new Intl.DateTimeFormat("zh-TW", {
        timeZone: "UTC",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        weekday: "short",
      }).format(nextHoliday);
      const holidayRemainingDays = Math.round((nextHoliday - today) / 86400000);
      holidayDateElement.dateTime = holidayIsoDate;
      holidayDateElement.textContent = holidayDisplayDate;
      holidayCountdownElement.textContent = holidayRemainingDays === 0
        ? "今天休市"
        : `還剩 ${holidayRemainingDays} 天`;
    } catch (error) {
      holidayDateElement.textContent = "暫時無法讀取";
      holidayCountdownElement.textContent = "官方行事曆讀取失敗";
    }
  };

  renderNextMarketHoliday();
})();
