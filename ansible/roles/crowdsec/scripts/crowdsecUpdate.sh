# Copy current crontab
crontab -l > crontab_new

# Add update and upgrade crontab
echo "0 7 * * * docker exec crowdsec cscli hub update && docker exec crowdsec cscli hub upgrade" >> crontab_new

# Commit and Cleanup
crontab crontab_new
rm crontab_new
