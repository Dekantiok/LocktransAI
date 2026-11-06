(function () {
        var amountInput = document.getElementById('id_amount') || document.querySelector('input[name="amount"]');
        var payerSelect = document.getElementById('id_fee_payer') || document.querySelector('select[name="fee_payer"]');
        var feeEl = document.getElementById('est-fee');
        var buyerEl = document.getElementById('est-buyer-total');
        var sellerEl = document.getElementById('est-seller-net');

        function formatUSD(num) {
            return '$' + Number(num).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        }

        function calculateFee(val) {
            if (val <= 50) return 1.50;
            if (val <= 100) return 2.00;
            if (val <= 500) return 2.50;
            if (val <= 1000) return 5.00;
            return 10.00;
        }

        function updateEstimate() {
            var val = parseFloat(amountInput ? amountInput.value : 0);
            if (isNaN(val) || val < 5) {
                if (feeEl) feeEl.textContent = '$1.50';
                if (buyerEl) buyerEl.textContent = '-';
                if (sellerEl) sellerEl.textContent = '-';
                return;
            }
            var fee = calculateFee(val);
            var payer = payerSelect ? payerSelect.value : 'buyer';
            var buyerTotal = payer === 'buyer' ? (val + fee) : val;
            var sellerNet = payer === 'seller' ? Math.max(0, val - fee) : val;

            if (feeEl) feeEl.textContent = formatUSD(fee);
            if (buyerEl) buyerEl.textContent = formatUSD(buyerTotal);
            if (sellerEl) sellerEl.textContent = formatUSD(sellerNet);
        }

        if (amountInput) amountInput.addEventListener('input', updateEstimate);
        if (payerSelect) payerSelect.addEventListener('change', updateEstimate);
        updateEstimate();
    })();
