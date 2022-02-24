# Copy current crontab
crontab -l > crontab_new

# Declare Var
CRONJOB="15 7 * * * docker exec crowdsec cscli hub update && docker exec crowdsec cscli hub upgrade"

# Add update and upgrade crontab at 7:15am UTC(2:15am EST)
if grep -q "$CRONJOB" crontab_new; then
echo "$CRONJOB" >> crontab_new
fi

# Commit and Cleanup
crontab crontab_new
rm crontab_new
