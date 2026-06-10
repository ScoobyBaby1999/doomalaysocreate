// ═══════════════════════════════════════════════════
// CALENDAR — dynamic month grid with task rendering
// ═══════════════════════════════════════════════════

let currentDate = new Date();

function generateCalendar() {
  const calendar = document.getElementById("calendar");
  calendar.innerHTML = "";

  const year = currentDate.getFullYear();
  const month = currentDate.getMonth();

  const title = document.getElementById("calendarTitle");
  if (title) {
    title.textContent =
      currentDate.toLocaleString("default", { month: "long" }) + " " + year;
  }

  const firstDay = new Date(year, month, 1).getDay();
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const today = new Date();

  // Empty cells before first day
  for (let i = 0; i < firstDay; i++) {
    const empty = document.createElement("div");
    empty.className = "day empty";
    calendar.appendChild(empty);
  }

  // Day cells
  for (let day = 1; day <= daysInMonth; day++) {
    const cell = document.createElement("div");
    cell.className = "day";

    const fullDate =
      year + "-" +
      String(month + 1).padStart(2, "0") + "-" +
      String(day).padStart(2, "0");

    // Highlight today
    if (
      day === today.getDate() &&
      month === today.getMonth() &&
      year === today.getFullYear()
    ) {
      cell.classList.add("today");
    }

    let html = `<span class="day-number">${day}</span>`;

    notes.forEach(n => {
      if (n.date === fullDate) {
        html += `<div class="task">${n.title || "Untitled"}</div>`;
      }
    });

    cell.innerHTML = html;
    calendar.appendChild(cell);
  }
}

function prevMonth() {
  currentDate.setMonth(currentDate.getMonth() - 1);
  generateCalendar();
}

function nextMonth() {
  currentDate.setMonth(currentDate.getMonth() + 1);
  generateCalendar();
}
