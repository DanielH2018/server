# Copy current crontab
crontab -l > crontab_new

# Add update and upgrade crontab at 7:15am UTC(2:15am EST)
echo "15 7 * * * docker exec crowdsec cscli hub update && docker exec crowdsec cscli hub upgrade" >> crontab_new

# Commit and Cleanup
crontab crontab_new
rm crontab_new
