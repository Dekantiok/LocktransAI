        document.addEventListener('click', function (event) {
            var button = event.target.closest('[data-copy]');
            if (!button) return;
            var value = button.getAttribute('data-copy');
            var done = function () {
                var original = button.innerHTML;
                var hasText = button.textContent.trim().length > 0;
                button.innerHTML = hasText
                    ? '<svg class="tc-icon me-1" aria-hidden="true" focusable="false" width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><path d="M12.736 3.97a.733.733 0 0 1 1.047 0c.286.289.29.756.01 1.05L7.88 12.01a.733.733 0 0 1-1.065.02L3.217 8.384a.757.757 0 0 1 0-1.06.733.733 0 0 1 1.047 0l3.052 3.093 5.4-6.425z"/></svg>Copied'
                    : '<svg class="tc-icon" aria-hidden="true" focusable="false" width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><path d="M12.736 3.97a.733.733 0 0 1 1.047 0c.286.289.29.756.01 1.05L7.88 12.01a.733.733 0 0 1-1.065.02L3.217 8.384a.757.757 0 0 1 0-1.06.733.733 0 0 1 1.047 0l3.052 3.093 5.4-6.425z"/></svg>';
                button.classList.add('copied');
                setTimeout(function () {
                    button.innerHTML = original;
                    button.classList.remove('copied');
                }, 1500);
            };
            if (navigator.clipboard) {
                navigator.clipboard.writeText(value).then(done);
            } else {
                var input = document.createElement('textarea');
                input.value = value;
                document.body.appendChild(input);
                input.select();
                document.execCommand('copy');
                document.body.removeChild(input);
                done();
            }
        });

        function tickCountdowns() {
            document.querySelectorAll('[data-countdown]').forEach(function (el) {
                var target = document.querySelector('[data-deadline]');
                if (!target) return;
                var deadline = new Date(target.getAttribute('data-deadline'));
                if (isNaN(deadline)) return;
                var remaining = Math.max(0, deadline - new Date());
                var mins = Math.floor(remaining / 60000);
                var hours = Math.floor(mins / 60);
                el.hidden = false;
                el.textContent = remaining === 0
                    ? 'Expiring'
                    : (hours >= 24
                        ? Math.floor(hours / 24) + 'd ' + (hours % 24) + 'h left'
                        : hours + 'h ' + (mins % 60) + 'm left');
            });
        }
        document.addEventListener('DOMContentLoaded', tickCountdowns);
        setInterval(tickCountdowns, 30000);
        document.body.addEventListener('htmx:afterSwap', tickCountdowns);
